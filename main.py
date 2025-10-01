import re
import time
import numpy as np
import asyncio
import pyaudio
import os
import sys
from dotenv import load_dotenv
from collections import deque

# Importações do Google Cloud
from google.cloud import speech_v1p1beta1 as speech
from google.cloud import translate

# Importação da sua API de Apresentação
from presenter_api import send_text_to_presenter

load_dotenv()

# Configurações do Áudio
FORMAT = pyaudio.paInt16
CHANNELS = int(os.getenv("CHANNELS"))
RATE = int(os.getenv("RATE"))
CHUNK = int(os.getenv("CHUNK"))

SILENCE_THRESHOLD = float(os.getenv("SILENCE_THRESHOLD"))
# Para pregação com intérprete, queremos detectar pausas curtas (2-3 segundos)
PAUSE_DETECTION_TIME = float(os.getenv("PAUSE_DETECTION_TIME", "2.0"))
GCP_PROJECT_ID = os.getenv('GCP_PROJECT_ID')

# Modo de operação: "interpreter" ou "continuous"
OPERATION_MODE = os.getenv("OPERATION_MODE", "interpreter").lower()

# Limite de palavras para forçar commit (evita previews gigantes)
MAX_WORDS_BEFORE_COMMIT = int(os.getenv("MAX_WORDS_BEFORE_COMMIT", "12"))

# Eventos de controle
STOP = asyncio.Event()
RUNNING = asyncio.Event()
RUNNING.set()

# Estatísticas da sessão
session_stats = {
    'frases_transcritas': 0,
    'frases_traduzidas': 0,
    'inicio': time.time()
}

def translate_text_with_google_cloud(text: str) -> str:
    """
    Traduz o texto usando a Google Cloud Translation API.
    Otimizado para frases completas de pregação.
    """
    if not text or len(text.strip()) < 3:
        return text
    
    try:
        client = translate.TranslationServiceClient()
        parent = f"projects/{GCP_PROJECT_ID}/locations/global"
        
        # Pré-processa o texto para melhorar referências bíblicas
        processed_text = preprocess_biblical_references(text)
        
        response = client.translate_text(
            parent=parent,
            contents=[processed_text],
            source_language_code="en",
            target_language_code="pt-BR",
            mime_type="text/plain"
        )
        
        if response.translations:
            session_stats['frases_traduzidas'] += 1
            translated = response.translations[0].translated_text
            # Pós-processa a tradução para corrigir referências
            return postprocess_biblical_references(translated)
        return text
        
    except Exception as e:
        print(f"\n[ERRO TRADUÇÃO] {e}")
        return text

def preprocess_biblical_references(text: str) -> str:
    """
    Pré-processa o texto em inglês para melhorar o reconhecimento de referências bíblicas.
    Converte padrões comuns de erro para o formato correto.
    """
   
    # Dicionário de correções comuns (erros de transcrição → correto)
    common_errors = {
        r'\bnumber\s+eleven\b': 'Numbers 11',
        r'\bnumbers?\s+(\w+)\s+(\d+)': lambda m: f'Numbers {word_to_number(m.group(1))}:{m.group(2)}',
        r'\b(genesis|exodus|leviticus|deuteronomy)\s+(\w+)\s+(\d+)': 
            lambda m: f'{m.group(1).title()} {word_to_number(m.group(2))}:{m.group(3)}',
    }
    
    text_lower = text.lower()
    result = text
    
    # Padrão: "Nome_do_Livro palavra_número número"
    # Ex: "Numbers eleven seventeen" → "Numbers 11:17"
    bible_pattern = re.compile(
        r'\b(genesis|exodus|leviticus|numbers|deuteronomy|joshua|judges|ruth|samuel|kings|chronicles|'
        r'ezra|nehemiah|esther|job|psalms|proverbs|ecclesiastes|isaiah|jeremiah|ezekiel|daniel|'
        r'hosea|joel|amos|obadiah|jonah|micah|nahum|habakkuk|zephaniah|haggai|zechariah|malachi|'
        r'matthew|mark|luke|john|acts|romans|corinthians|galatians|ephesians|philippians|'
        r'colossians|thessalonians|timothy|titus|philemon|hebrews|james|peter|jude|revelation)\s+'
        r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|'
        r'sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)\s+'
        r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|'
        r'sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|'
        r'\d+)',
        re.IGNORECASE
    )
    
    def replace_reference(match):
        book = match.group(1).title()
        chapter = word_to_number(match.group(2))
        verse = match.group(3)
        if verse.isdigit():
            verse_num = verse
        else:
            verse_num = str(word_to_number(verse))
        return f'{book} {chapter}:{verse_num}'
    
    result = bible_pattern.sub(replace_reference, result)
    
    return result

def word_to_number(word: str) -> int:
    """
    Converte palavra numérica em inglês para número.
    Ex: "eleven" → 11, "seventeen" → 17
    """
    word = word.lower().strip()
    
    numbers = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
        'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
        'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80,
        'ninety': 90, 'hundred': 100
    }
    
    if word in numbers:
        return numbers[word]
    
    # Se já for número, retorna
    if word.isdigit():
        return int(word)
    
    return word  # Retorna original se não conseguir converter

def postprocess_biblical_references(text: str) -> str:
    """
    Pós-processa a tradução para corrigir nomes de livros bíblicos em português.
    """
   
    # Mapeamento de nomes em inglês para português
    book_translations = {
        'Genesis': 'Gênesis',
        'Exodus': 'Êxodo',
        'Leviticus': 'Levítico',
        'Numbers': 'Números',
        'Deuteronomy': 'Deuteronômio',
        'Joshua': 'Josué',
        'Judges': 'Juízes',
        'Samuel': 'Samuel',
        'Kings': 'Reis',
        'Chronicles': 'Crônicas',
        'Psalms': 'Salmos',
        'Proverbs': 'Provérbios',
        'Ecclesiastes': 'Eclesiastes',
        'Isaiah': 'Isaías',
        'Jeremiah': 'Jeremias',
        'Ezekiel': 'Ezequiel',
        'Daniel': 'Daniel',
        'Matthew': 'Mateus',
        'Mark': 'Marcos',
        'Luke': 'Lucas',
        'John': 'João',
        'Acts': 'Atos',
        'Romans': 'Romanos',
        'Corinthians': 'Coríntios',
        'Galatians': 'Gálatas',
        'Ephesians': 'Efésios',
        'Philippians': 'Filipenses',
        'Colossians': 'Colossenses',
        'Thessalonians': 'Tessalonicenses',
        'Timothy': 'Timóteo',
        'Hebrews': 'Hebreus',
        'James': 'Tiago',
        'Peter': 'Pedro',
        'Revelation': 'Apocalipse'
    }
    
    result = text
    
    # Substitui nomes de livros
    for eng, por in book_translations.items():
        # Padrão: Nome seguido de número:número
        pattern = rf'\b{eng}\s+(\d+):(\d+)\b'
        replacement = rf'{por} \1:\2'
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    
    return result

def format_text_for_display(text, max_chars_per_line=55, max_lines=2):
    """
    Formata texto para exibição no ProPresenter.
    Otimizado para frases de pregação (geralmente curtas).
    """
    if not text:
        return ""
    
    # Remove espaços extras
    text = ' '.join(text.split())
    
    if len(text) <= max_chars_per_line:
        return text

    words = text.split()
    lines = []
    current_line = []
    current_length = 0

    for word in words:
        word_length = len(word) + (1 if current_line else 0)
        if current_length + word_length <= max_chars_per_line:
            current_line.append(word)
            current_length += word_length
        else:
            if current_line:
                lines.append(' '.join(current_line))
                if len(lines) >= max_lines:
                    break
            current_line = [word]
            current_length = len(word)
    
    if current_line and len(lines) < max_lines:
        lines.append(' '.join(current_line))
    
    return '\n'.join(lines)

async def monitor_keyboard():
    """
    Escuta comandos do teclado de forma compatível com PyInstaller.
    Usa input() em thread separada para evitar travamentos.
    """
    global OPERATION_MODE
    
    loop = asyncio.get_running_loop()
    print("\n" + "="*60)
    print("  CONTROLES DO SISTEMA")
    print("="*60)
    print("  'p' + ENTER - Pausar/Retomar transcrição")
    print("  'c' + ENTER - Limpar tela do ProPresenter")
    print("  's' + ENTER - Mostrar estatísticas da sessão")
    print("  'm' + ENTER - Alternar modo (Intérprete ↔ Contínuo)")
    print("  'q' + ENTER - Sair do sistema")
    print("="*60 + "\n")
    
    def read_input():
        """Função bloqueante para ler input - roda em thread separada."""
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return None
    
    while not STOP.is_set():
        try:
            # Executa input() em thread separada para não bloquear o loop
            line = await loop.run_in_executor(None, read_input)
            
            if line is None:
                await asyncio.sleep(0.1)
                continue
            
            key = line.strip().lower()
            
            if key == "q":
                print("\n[SISTEMA] Encerrando sistema...")
                STOP.set()
                RUNNING.set()
                break
            elif key == "p":
                if RUNNING.is_set():
                    RUNNING.clear()
                    print("\n[SISTEMA] ⏸ PAUSADO - Digite 'p' + ENTER para retomar")
                else:
                    RUNNING.set()
                    print("\n[SISTEMA] ▶ RETOMADO")
            elif key == "c":
                print("\n[SISTEMA] 🗑 Limpando tela do ProPresenter...")
                try:
                    send_text_to_presenter("")
                    print("[SISTEMA] ✓ Tela limpa")
                except Exception as e:
                    print(f"[ERRO] Não foi possível limpar: {e}")
            elif key == "s":
                elapsed = time.time() - session_stats['inicio']
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                print("\n" + "="*60)
                print("  ESTATÍSTICAS DA SESSÃO")
                print("="*60)
                print(f"  Modo: {OPERATION_MODE.upper()}")
                print(f"  Tempo decorrido: {mins}min {secs}s")
                print(f"  Frases transcritas: {session_stats['frases_transcritas']}")
                print(f"  Frases traduzidas: {session_stats['frases_traduzidas']}")
                print("="*60 + "\n")
            elif key == "m":
                OPERATION_MODE = "continuous" if OPERATION_MODE == "interpreter" else "interpreter"
                print(f"\n[SISTEMA] 🔄 Modo alterado para: {OPERATION_MODE.upper()}")
                print(f"  {'→ Otimizado para pregação com pausas para intérprete' if OPERATION_MODE == 'interpreter' else '→ Otimizado para pregação contínua sem pausas'}\n")
            elif key == "":
                # Ignora ENTER vazio
                pass
            else:
                print(f"[AVISO] Comando '{key}' não reconhecido. Use: p, c, s, m ou q")
                
        except Exception as e:
            print(f"\n[ERRO MONITOR] {e}")
            await asyncio.sleep(0.5)
        
        await asyncio.sleep(0.1)

def audio_generator(stream):
    """
    Captura áudio do microfone.
    Otimizado para detectar pausas entre pregador e intérprete.
    """
    consecutive_silence = 0
    silence_chunks_needed = int((PAUSE_DETECTION_TIME * RATE) / CHUNK)
    
    while not STOP.is_set():
        if not RUNNING.is_set():
            time.sleep(0.1)
            continue

        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
        except IOError as e:
            print(f"\n[ERRO ÁUDIO] {e}")
            continue

        if not data:
            continue

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples**2)) / 32768.0

        if rms > SILENCE_THRESHOLD:
            consecutive_silence = 0
            yield data
        else:
            consecutive_silence += 1
            # Mesmo em silêncio, envia alguns chunks para o Google detectar pausa
            if consecutive_silence < silence_chunks_needed:
                yield data
        
        time.sleep(0.002)

async def transcribe_stream(stream):
    """
    Transcrição otimizada para pregação com intérprete.
    Foco em frases curtas e pausas naturais.
    """
    client = speech.SpeechAsyncClient()

    # CONFIGURAÇÃO OTIMIZADA PARA PREGAÇÃO COM INTÉRPRETE
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code="en-US",
        
        # Essencial para pregações
        enable_automatic_punctuation=True,
        
        # Modelo ideal para frases curtas de pregação
        # "command_and_search" é ótimo para frases curtas (2-10 palavras)
        model="command_and_search",
        
        # Use enhanced para melhor qualidade (custo extra, mas vale a pena)
        # use_enhanced=True,
        
        # Profanity filter desligado (pode bloquear palavras bíblicas)
        profanity_filter=False,
        
        # Configuração de timeout para detectar pausas do intérprete
        speech_contexts=[
            speech.SpeechContext(
                # Adicione vocabulário bíblico comum se necessário
                phrases=[
                    "Jesus", "Christ", "God", "Lord", "Bible", 
                    "Gospel", "Faith", "Prayer", "Amen", "Hallelujah",
                    "cure", "angels", "Spirit", "Holy Spirit","Pentecost",
                    # Livros do Antigo Testamento
                    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy",
                    "Joshua", "Judges", "Ruth", "Samuel", "Kings", "Chronicles",
                    "Ezra", "Nehemiah", "Esther", "Job", "Psalms", "Proverbs",
                    "Ecclesiastes", "Song of Solomon", "Isaiah", "Jeremiah",
                    "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel",
                    "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
                    "Zephaniah", "Haggai", "Zechariah", "Malachi",
                    # Livros do Novo Testamento
                    "Matthew", "Mark", "Luke", "John", "Acts", "Romans",
                    "Corinthians", "Galatians", "Ephesians", "Philippians",
                    "Colossians", "Thessalonians", "Timothy", "Titus",
                    "Philemon", "Hebrews", "James", "Peter", "Revelation",
                    # Frases de referência comum
                    "chapter", "verse", "verses", "says", "tells us"
                ],
                boost=15.0  # Aumenta bastante a probabilidade dessas palavras
            )
        ]
    )

    streaming_config = speech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True,  # Mostra progresso em tempo real
        single_utterance=False  # Permite múltiplas frases na mesma sessão
    )

    def audio_requests():
        yield speech.StreamingRecognizeRequest(streaming_config=streaming_config)
        for chunk in audio_generator(stream):
            if STOP.is_set():
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    responses = await client.streaming_recognize(requests=audio_requests())
    
    print("[SISTEMA] ✓ Transcrição iniciada - Aguardando pregador...\n")

    # Variáveis de controle
    current_sentence = ""
    last_final_time = time.time()
    sentence_buffer = deque(maxlen=3)  # Mantém últimas 3 frases para contexto
    
    async for response in responses:
        if STOP.is_set():
            break
        
        if not response.results:
            continue

        result = response.results[0]
        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript.strip()
        confidence = result.alternatives[0].confidence if result.is_final else 0
        
        if not transcript:
            continue

        # RESULTADO FINAL - Frase completa do pregador
        if result.is_final:
            # Evita duplicatas
            if transcript == current_sentence:
                continue
            
            session_stats['frases_transcritas'] += 1
            current_sentence = transcript
            last_final_time = time.time()
            
            # Adiciona ao buffer de contexto
            sentence_buffer.append(transcript)
            
            # Log da transcrição
            confidence_pct = int(confidence * 100) if confidence > 0 else 0
            print(f"\n{'─'*60}")
            print(f"[PREGADOR] {transcript}")
            if confidence > 0:
                print(f"[CONFIANÇA] {confidence_pct}%")
            
            # Traduz imediatamente
            print("[TRADUZINDO...]", end=" ")
            translated = translate_text_with_google_cloud(transcript)
            print("✓")
            print(f"[LEGENDA] {translated}")
            print(f"{'─'*60}\n")
            
            # Envia para ProPresenter
            formatted = format_text_for_display(translated)
            send_text_to_presenter(formatted)
            
            # Pequena pausa para sincronizar com o intérprete
            await asyncio.sleep(0.3)
            
        else:
            # RESULTADO PARCIAL - Mostra progresso enquanto pregador fala
            time_since_last = time.time() - last_final_time
            
            words = transcript.split()
            word_count = len(words)
            
            # Detecta pontuação que indica fim de pensamento
            has_punctuation = any(p in transcript for p in ['. ', '? ', '! '])
            
            # ESTRATÉGIA ANTI-PREVIEW-GIGANTE:
            # Força commit em frases longas para evitar acúmulo
            
            # 1. PRIORIDADE MÁXIMA: Detecta pontuação e comita até ela
            if has_punctuation and word_count >= 5:
                # Pega até o último ponto/exclamação/interrogação
                last_punct_idx = max(
                    transcript.rfind('. '),
                    transcript.rfind('? '),
                    transcript.rfind('! ')
                )
                
                if last_punct_idx > 0:
                    complete_part = transcript[:last_punct_idx + 1].strip()
                    
                    # Evita duplicatas e frases muito curtas
                    if complete_part != current_sentence and len(complete_part) > 10:
                        print(f"\n{'─'*60}")
                        print(f"[FRASE DETECTADA] {complete_part[:80]}{'...' if len(complete_part) > 80 else ''}")
                        
                        translated = translate_text_with_google_cloud(complete_part)
                        print(f"[LEGENDA] {translated[:80]}{'...' if len(translated) > 80 else ''}")
                        print(f"{'─'*60}\n")
                        
                        formatted = format_text_for_display(translated)
                        send_text_to_presenter(formatted)
                        
                        current_sentence = complete_part
                        session_stats['frases_transcritas'] += 1
                        last_final_time = time.time()  # Reseta o timer
            
            # 2. LIMITE DE PALAVRAS: Se passar de X palavras, força commit
            elif word_count >= MAX_WORDS_BEFORE_COMMIT:
                # Evita duplicatas
                if transcript != current_sentence and len(transcript) > 20:
                    print(f"\n{'─'*60}")
                    print(f"[FORÇANDO COMMIT] {word_count} palavras")
                    print(f"[TEXTO] {transcript[:80]}{'...' if len(transcript) > 80 else ''}")
                    
                    translated = translate_text_with_google_cloud(transcript)
                    print(f"[LEGENDA] {translated[:80]}{'...' if len(translated) > 80 else ''}")
                    print(f"{'─'*60}\n")
                    
                    formatted = format_text_for_display(translated)
                    send_text_to_presenter(formatted)
                    
                    current_sentence = transcript
                    session_stats['frases_transcritas'] += 1
                    last_final_time = time.time()
            
            # 3. PREVIEW SIMPLES: Apenas mostra progresso (sem traduzir)
            elif word_count >= 3:
                # Mostra apenas primeiras 50 chars para não poluir
                preview_text = transcript if len(transcript) <= 50 else transcript[:50] + "..."
                print(f"[•••] {preview_text}", end='\r')

    print("\n[SISTEMA] Stream de transcrição encerrado.")

async def main():
    """Função principal do sistema."""
    print("\n" + "="*60)
    print("  SISTEMA DE LEGENDAS PARA PREGAÇÃO COM INTÉRPRETE")
    print("  Transcrição (EN) → Tradução (PT-BR) → ProPresenter")
    print("="*60)
    
    # Validação
    if not GCP_PROJECT_ID:
        print("\n[ERRO] Configure GCP_PROJECT_ID no arquivo .env")
        return
    
    # Inicializa áudio
    audio_system = pyaudio.PyAudio()
    
    print(f"\n[CONFIGURAÇÃO DE ÁUDIO]")
    print(f"  Modo de operação: {OPERATION_MODE.upper()}")
    if OPERATION_MODE == "interpreter":
        print(f"    → Otimizado para pregador com intérprete (frases curtas)")
    else:
        print(f"    → Otimizado para pregação contínua")
    print(f"  Taxa de amostragem: {RATE} Hz")
    print(f"  Canais: {CHANNELS}")
    print(f"  Tamanho do chunk: {CHUNK} frames")
    print(f"  Limiar de silêncio: {SILENCE_THRESHOLD}")
    print(f"  Detecção de pausa: {PAUSE_DETECTION_TIME}s")
    print(f"  Limite de palavras: {MAX_WORDS_BEFORE_COMMIT} (força commit após esse limite)")
    
    try:
        device_index = int(os.getenv("INPUT_DEVICE_INDEX"))
        device_info = audio_system.get_device_info_by_index(device_index)
        print(f"  Dispositivo: {device_info['name']}")
    except:
        print(f"  Dispositivo: Padrão do sistema")
        device_index = None
    
    stream = audio_system.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=CHUNK
    )

    print("\n[STATUS] Sistema pronto! Aguardando início da pregação...")
    print("[DICA] O pregador deve falar frases curtas e pausar para o intérprete\n")

    try:
        await asyncio.gather(
            monitor_keyboard(),
            transcribe_stream(stream)
        )
    except KeyboardInterrupt:
        print("\n\n[SISTEMA] Interrompido pelo usuário (Ctrl+C)")
        STOP.set()
    except asyncio.CancelledError:
        print("\n\n[SISTEMA] Tarefas canceladas")
    except Exception as e:
        print(f"\n\n[ERRO CRÍTICO] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n[FINALIZANDO]")
        
        # Força o encerramento de todas as tarefas pendentes
        STOP.set()
        RUNNING.set()
        
        # Mostra estatísticas finais
        try:
            elapsed = time.time() - session_stats['inicio']
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            print(f"  Duração da sessão: {mins}min {secs}s")
            print(f"  Total de frases: {session_stats['frases_transcritas']}")
            print(f"  Total traduzido: {session_stats['frases_traduzidas']}")
        except:
            pass
        
        # Fecha o stream de áudio com segurança
        try:
            if stream.is_active():
                stream.stop_stream()
            stream.close()
        except Exception as e:
            print(f"[AVISO] Erro ao fechar stream: {e}")
        
        try:
            audio_system.terminate()
        except Exception as e:
            print(f"[AVISO] Erro ao terminar PyAudio: {e}")
        
        # Limpa a tela do ProPresenter
        try:
            send_text_to_presenter("")
        except:
            pass
        
        print("\n[SISTEMA] ✓ Encerrado com sucesso")
        print("="*60 + "\n")
        
        # Pequena pausa antes de fechar (para ler mensagens)
        time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())