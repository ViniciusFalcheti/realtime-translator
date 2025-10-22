import time
import numpy as np
import asyncio
import pyaudio
import os
import sys
import re
from dotenv import load_dotenv
from collections import deque

# Importações do Google Cloud
from google.cloud import speech_v1p1beta1 as speech
from google.cloud import translate

# Importação da sua API de Apresentação
from presenter_api import send_text_to_presenter

# CORREÇÃO PARA PYINSTALLER --onefile
# Quando rodando como executável, o diretório de trabalho é diferente
if getattr(sys, 'frozen', False):
    # Rodando como executável (PyInstaller)
    application_path = os.path.dirname(sys.executable)
else:
    # Rodando como script Python normal
    application_path = os.path.dirname(os.path.abspath(__file__))

# Muda para o diretório do executável para encontrar .env e credenciais
os.chdir(application_path)
print(f"[SISTEMA] Diretório de trabalho: {application_path}")

load_dotenv()

# Configurações do Áudio
FORMAT = pyaudio.paInt16
CHANNELS = int(os.getenv("CHANNELS"))
RATE = int(os.getenv("RATE"))
CHUNK = int(os.getenv("CHUNK"))

SILENCE_THRESHOLD = float(os.getenv("SILENCE_THRESHOLD"))
MAX_SILENCE_TIME  = float(os.getenv("MAX_SILENCE_TIME", "2.0"))
PAUSE_DETECTION_TIME = float(os.getenv("PAUSE_DETECTION_TIME", "2.0"))
GCP_PROJECT_ID = os.getenv('GCP_PROJECT_ID')

# Modo de operação: "interpreter" ou "continuous"
OPERATION_MODE = os.getenv("OPERATION_MODE", "interpreter").lower()

# Limite de palavras para forçar commit (evita previews gigantes)
MAX_WORDS_BEFORE_COMMIT = int(os.getenv("MAX_WORDS_BEFORE_COMMIT", "12"))

# Modo DEBUG para diagnóstico de problemas
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Eventos de controle
STOP = asyncio.Event()
RUNNING = asyncio.Event()
RUNNING.set()

# Estatísticas da sessão
session_stats = {
    'frases_transcritas': 0,
    'frases_traduzidas': 0,
    'erros_traducao': 0,
    'inicio': time.time(),
    'ultima_atividade': time.time()
}

def translate_text_with_google_cloud(text: str) -> str:
    """
    Traduz o texto usando a Google Cloud Translation API.
    Otimizado para frases completas de pregação.
    """
    if not text or len(text.strip()) < 3:
        return text
    
    print(f"[TRADUÇÃO] Iniciando...", end=" ", flush=True)
    
    try:
        # Atualiza timestamp de última atividade
        session_stats['ultima_atividade'] = time.time()
        
        client = translate.TranslationServiceClient()
        parent = f"projects/{GCP_PROJECT_ID}/locations/global"
        
        # Pré-processa o texto para melhorar referências bíblicas
        processed_text = preprocess_biblical_references(text)
        print(f"processado...", end=" ", flush=True)
        
        print(f"enviando Google...", end=" ", flush=True)
        
        response = client.translate_text(
            request={
                "parent": parent,
                "contents": [processed_text],
                "source_language_code": "en",
                "target_language_code": "pt-BR",
                "mime_type": "text/plain"
            },
            timeout=5.0
        )
        
        print(f"recebendo...", end=" ", flush=True)
        
        if response.translations:
            session_stats['frases_traduzidas'] += 1
            session_stats['ultima_atividade'] = time.time()
            translated = response.translations[0].translated_text
            result = postprocess_biblical_references(translated)
            print(f"✓")
            if DEBUG_MODE:
                print(f"[DEBUG] '{text}' → '{result}'")
            return result
        
        print(f"⚠ (vazio)")
        session_stats['erros_traducao'] += 1
        return text
        
    except Exception as e:
        print(f"✗")
        session_stats['erros_traducao'] += 1
        session_stats['ultima_atividade'] = time.time()
        
        error_type = type(e).__name__
        error_msg = str(e)[:200]
        
        print(f"\n[ERRO TRADUÇÃO] {error_type}")
        
        if "403" in error_msg or "Permission" in error_msg:
            print(f"[CAUSA] Translation API não tem permissão ou não está habilitada")
            print(f"[SOLUÇÃO] Habilite a Translation API no Google Cloud Console")
        elif "timeout" in error_msg.lower() or "deadline" in error_msg.lower():
            print(f"[CAUSA] Timeout - resposta demorou mais de 5 segundos")
            print(f"[SOLUÇÃO] Verifique conexão de internet")
        else:
            print(f"[DETALHES] {error_msg}")
        
        print(f"[AVISO] Usando texto em inglês")
        return text

def format_text_for_display(text, max_chars_per_line=55, max_lines=2):
    """
    Formata texto para exibição no ProPresenter.
    Otimizado para frases de pregação (geralmente curtas).
    """
    if not text:
        return ""
    
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
                
                tempo_inativo = time.time() - session_stats['ultima_atividade']
                
                print("\n" + "="*60)
                print("  ESTATÍSTICAS DA SESSÃO")
                print("="*60)
                print(f"  Modo: {OPERATION_MODE.upper()}")
                print(f"  Tempo decorrido: {mins}min {secs}s")
                print(f"  Frases transcritas: {session_stats['frases_transcritas']}")
                print(f"  Frases traduzidas: {session_stats['frases_traduzidas']}")
                print(f"  Erros de tradução: {session_stats['erros_traducao']}")
                print(f"  Tempo desde última atividade: {int(tempo_inativo)}s")
                print("="*60 + "\n")
            elif key == "m":
                OPERATION_MODE = "continuous" if OPERATION_MODE == "interpreter" else "interpreter"
                print(f"\n[SISTEMA] 🔄 Modo alterado para: {OPERATION_MODE.upper()}")
                print(f"  {'→ Otimizado para pregação com pausas para intérprete' if OPERATION_MODE == 'interpreter' else '→ Otimizado para pregação contínua sem pausas'}\n")
            elif key == "":
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
    Converte automaticamente de 48kHz estéreo (Dante) para 16kHz mono (Google Cloud).
    """
    consecutive_silence = 0
    silence_chunks_needed = int((PAUSE_DETECTION_TIME * RATE) / CHUNK)
    
    # Detecta se precisa fazer conversão (Dante → Google Cloud)
    needs_conversion = RATE != 16000 or CHANNELS != 1
    target_rate = 16000  # Google Cloud prefere 16kHz
    
    if needs_conversion:
        print(f"[INFO] Conversão automática ativada: {RATE}Hz {CHANNELS}ch → {target_rate}Hz 1ch\n")
    
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

        try:
            # Converte bytes para array numpy
            samples = np.frombuffer(data, dtype=np.int16)
            
            if CHANNELS == 2:
                # Estéreo: converte para mono
                # Verifica se há samples suficientes
                if len(samples) < 2:
                    continue
                
                # Reshape para (frames, channels)
                num_frames = len(samples) // 2
                samples = samples[:num_frames * 2]  # Garante tamanho par
                samples = samples.reshape(-1, 2)
                
                # Média dos dois canais
                samples_mono = samples.mean(axis=1).astype(np.int16)
            else:
                # Já é mono
                samples_mono = samples
            
            # Converte taxa de amostragem se necessário (48kHz → 16kHz)
            if RATE != target_rate:
                num_samples_original = len(samples_mono)
                if num_samples_original == 0:
                    continue
                
                num_samples_target = int(num_samples_original * target_rate / RATE)
                
                if num_samples_target < 1:
                    continue
                
                # Índices para interpolação
                indices = np.linspace(0, num_samples_original - 1, num_samples_target)
                samples_resampled = np.interp(indices, np.arange(num_samples_original), samples_mono)
                samples_final = samples_resampled.astype(np.int16)
            else:
                samples_final = samples_mono
            
            if len(samples_final) == 0:
                continue
            
            # Calcula RMS do áudio processado
            rms = np.sqrt(np.mean(samples_final.astype(np.float32)**2)) / 32768.0

            if rms > SILENCE_THRESHOLD:
                consecutive_silence = 0
                # Converte de volta para bytes e envia
                yield samples_final.tobytes()
            else:
                consecutive_silence += 1
                if consecutive_silence < silence_chunks_needed:
                    yield samples_final.tobytes()
        
        except Exception as e:
            print(f"\n[ERRO CONVERSÃO] {type(e).__name__}: {e}")
            continue
        
        time.sleep(0.002)

def preprocess_biblical_references(text: str) -> str:
    """
    Pré-processa o texto em inglês para melhorar o reconhecimento de referências bíblicas.
    """
    result = text
    
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
    
    if word.isdigit():
        return int(word)
    
    return word

def postprocess_biblical_references(text: str) -> str:
    """
    Pós-processa a tradução para corrigir nomes de livros bíblicos em português.
    """
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
    
    for eng, por in book_translations.items():
        pattern = rf'\b{eng}\s+(\d+):(\d+)\b'
        replacement = rf'{por} \1:\2'
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    
    return result

async def transcribe_stream(stream):
    """
    Transcrição em tempo real com reconexão automática.
    """
    
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries and not STOP.is_set():
        try:
            client = speech.SpeechAsyncClient()

            recognition_config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,  # SEMPRE 16kHz para Google Cloud
                language_code="en-US",
                enable_automatic_punctuation=True,
                model="command_and_search",
                use_enhanced=True,
                profanity_filter=False,
                speech_contexts=[
                    speech.SpeechContext(
                        phrases=[
                            "Jesus", "Christ", "God", "Lord", "Bible", 
                            "Gospel", "Faith", "Prayer", "Amen", "Hallelujah",
                            "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy",
                            "Joshua", "Judges", "Ruth", "Samuel", "Kings", "Chronicles",
                            "Ezra", "Nehemiah", "Esther", "Job", "Psalms", "Proverbs",
                            "Ecclesiastes", "Song of Solomon", "Isaiah", "Jeremiah",
                            "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel",
                            "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
                            "Zephaniah", "Haggai", "Zechariah", "Malachi",
                            "Matthew", "Mark", "Luke", "John", "Acts", "Romans",
                            "Corinthians", "Galatians", "Ephesians", "Philippians",
                            "Colossians", "Thessalonians", "Timothy", "Titus",
                            "Philemon", "Hebrews", "James", "Peter", "Revelation",
                            "chapter", "verse", "verses", "says", "tells us"
                        ],
                        boost=15.0
                    )
                ]
            )

            streaming_config = speech.StreamingRecognitionConfig(
                config=recognition_config,
                interim_results=True,
                single_utterance=False
            )

            def audio_requests():
                yield speech.StreamingRecognizeRequest(streaming_config=streaming_config)
                for chunk in audio_generator(stream):
                    if STOP.is_set():
                        break
                    try:
                        yield speech.StreamingRecognizeRequest(audio_content=chunk)
                    except Exception as e:
                        print(f"\n[ERRO AUDIO CHUNK] {type(e).__name__}: {e}")
                        break

            responses = await client.streaming_recognize(
                requests=audio_requests(),
                timeout=300.0
            )
            
            if retry_count > 0:
                print(f"[SISTEMA] ✓ Reconectado com sucesso (tentativa {retry_count + 1})")
            else:
                print("[SISTEMA] ✓ Transcrição iniciada - Aguardando pregador...\n")

            last_final_text = "" 
            sentence_buffer = deque(maxlen=3)
            current_sentence = ""
            last_final_time = time.time()

            async for response in responses:
                if STOP.is_set():
                    break
                if not response.results or not response.results[0].alternatives:
                    continue

                result = response.results[0]
                if not result.alternatives:
                    continue

                transcript = result.alternatives[0].transcript.strip()
                confidence = result.alternatives[0].confidence if result.is_final else 0
                
                if not transcript:
                    continue

                if result.is_final:
                    if transcript == current_sentence:
                        continue
                    
                    session_stats['frases_transcritas'] += 1
                    current_sentence = transcript
                    last_final_time = time.time()
                    
                    sentence_buffer.append(transcript)
                    
                    confidence_pct = int(confidence * 100) if confidence > 0 else 0
                    print(f"\n{'─'*60}")
                    print(f"[PREGADOR] {transcript}")
                    if confidence > 0:
                        print(f"[CONFIANÇA] {confidence_pct}%")
                    
                    try:
                        print("[PROCESSANDO]", end=" ", flush=True)
                        translated = translate_text_with_google_cloud(transcript)
                        
                        if translated == transcript:
                            print("[AVISO] Usando texto original (tradução falhou)")
                        
                        print(f"[LEGENDA PT] {translated}")
                        print(f"{'─'*60}\n")
                        
                        try:
                            print("[PROPRESENTER]", end=" ", flush=True)
                            formatted = format_text_for_display(translated)
                            send_text_to_presenter(formatted)
                            print("✓")
                        except Exception as e:
                            print(f"✗")
                            print(f"[ERRO PROPRESENTER] {type(e).__name__}: {str(e)[:100]}")
                            print("[AVISO] Legenda não foi enviada, mas sistema continua...")
                        
                    except Exception as e:
                        print(f"✗")
                        print(f"[ERRO GERAL] {type(e).__name__}: {str(e)[:100]}")
                        print("[AVISO] Pulando esta frase, aguardando próxima...")
                    
                    await asyncio.sleep(0.3)
                    
                else:
                    words = transcript.split()
                    word_count = len(words)
                    
                    has_punctuation = any(p in transcript for p in ['. ', '? ', '! '])
                    
                    if has_punctuation and word_count >= 5:
                        last_punct_idx = max(
                            transcript.rfind('. '),
                            transcript.rfind('? '),
                            transcript.rfind('! ')
                        )
                        
                        if last_punct_idx > 0:
                            complete_part = transcript[:last_punct_idx + 1].strip()
                            
                            if complete_part != current_sentence and len(complete_part) > 10:
                                try:
                                    print(f"\n{'─'*60}")
                                    print(f"[FRASE DETECTADA] {complete_part[:80]}{'...' if len(complete_part) > 80 else ''}")
                                    
                                    translated = translate_text_with_google_cloud(complete_part)
                                    print(f"[LEGENDA] {translated[:80]}{'...' if len(translated) > 80 else ''}")
                                    print(f"{'─'*60}\n")
                                    
                                    try:
                                        formatted = format_text_for_display(translated)
                                        send_text_to_presenter(formatted)
                                    except Exception as e:
                                        print(f"[ERRO PROPRESENTER] {e}")
                                    
                                    current_sentence = complete_part
                                    session_stats['frases_transcritas'] += 1
                                    last_final_time = time.time()
                                except Exception as e:
                                    print(f"[ERRO] {type(e).__name__}: {e}")
                                    print("[AVISO] Continuando...")
                    
                    elif word_count >= MAX_WORDS_BEFORE_COMMIT:
                        if transcript != current_sentence and len(transcript) > 20:
                            try:
                                print(f"\n{'─'*60}")
                                print(f"[FORÇANDO COMMIT] {word_count} palavras")
                                print(f"[TEXTO] {transcript[:80]}{'...' if len(transcript) > 80 else ''}")
                                
                                translated = translate_text_with_google_cloud(transcript)
                                print(f"[LEGENDA] {translated[:80]}{'...' if len(translated) > 80 else ''}")
                                print(f"{'─'*60}\n")
                                
                                try:
                                    formatted = format_text_for_display(translated)
                                    send_text_to_presenter(formatted)
                                except Exception as e:
                                    print(f"[ERRO PROPRESENTER] {e}")
                                
                                current_sentence = transcript
                                session_stats['frases_transcritas'] += 1
                                last_final_time = time.time()
                            except Exception as e:
                                print(f"[ERRO] {type(e).__name__}: {e}")
                                print("[AVISO] Continuando...")
                    
                    elif word_count >= 3:
                        preview_text = transcript if len(transcript) <= 50 else transcript[:50] + "..."
                        print(f"[•••] {preview_text}", end='\r')

            print("\n[SISTEMA] Stream de transcrição encerrado.")
            break
            
        except Exception as e:
            retry_count += 1
            error_name = type(e).__name__
            error_msg = str(e)[:200]
            
            print(f"\n{'='*60}")
            print(f"[ERRO CRÍTICO] {error_name}")
            print(f"{'='*60}")
            print(f"Detalhes: {error_msg}")
            
            if "INTERNAL" in error_msg or "Internal error" in error_msg:
                print("\n🔍 CAUSA PROVÁVEL:")
                print("  - Problema na transmissão de áudio")
                print("  - Formato incompatível (taxa/canais/chunk)")
                print("\n💡 TENTANDO RECONECTAR...")
                
            elif "DEADLINE_EXCEEDED" in error_msg or "timeout" in error_msg.lower():
                print("\n🔍 CAUSA: Timeout")
                print("💡 Verificando conexão...")
                
            elif "UNAVAILABLE" in error_msg:
                print("\n🔍 CAUSA: Google Cloud temporariamente indisponível")
                print("💡 Tentando reconectar...")
            
            if retry_count < max_retries:
                wait_time = retry_count * 2
                print(f"\n⏳ Aguardando {wait_time}s antes de reconectar...")
                print(f"   (Tentativa {retry_count}/{max_retries})")
                await asyncio.sleep(wait_time)
                print("\n🔄 Reconectando...\n")
            else:
                print(f"\n❌ Máximo de tentativas alcançado ({max_retries})")
                print("   Sistema será encerrado.")
                print("\n🔧 SOLUÇÕES:")
                print("   1. Verifique conexão de internet")
                print("   2. Verifique RATE e CHUNK no .env")
                print("   3. Teste com INPUT_DEVICE_INDEX diferente")
                STOP.set()
                break

async def main():
    """Função principal do sistema."""
    print("\n" + "="*60)
    print("  SISTEMA DE TRANSCRIÇÃO E TRADUÇÃO EM TEMPO REAL")
    print("="*60)
    
    if not GCP_PROJECT_ID:
        print("\n[ERRO] GCP_PROJECT_ID não configurado!")
        print("Verifique o arquivo .env")
        input("Pressione ENTER para sair...")
        return
    
    creds_file = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'credenciais_google.json')
    creds_path = os.path.join(application_path, creds_file)
    
    print(f"\n[VERIFICAÇÃO] Procurando credenciais...")
    print(f"  Arquivo: {creds_file}")
    print(f"  Caminho completo: {creds_path}")
    
    if os.path.exists(creds_path):
        print(f"  ✓ Arquivo encontrado!")
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = creds_path
    elif os.path.exists(creds_file):
        print(f"  ✓ Arquivo encontrado (diretório atual)!")
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.abspath(creds_file)
    else:
        print(f"  ✗ ERRO: Arquivo de credenciais não encontrado!")
        print(f"\nColoque o arquivo '{creds_file}' na mesma pasta do executável:")
        print(f"  {application_path}")
        input("\nPressione ENTER para sair...")
        return
    
    print(f"\n[TESTE] Verificando conexão com Google Cloud...")
    try:
        test_client = speech.SpeechClient()
        print("  ✓ Speech-to-Text: Conectado")
    except Exception as e:
        print(f"  ✗ Speech-to-Text: ERRO")
        print(f"  Detalhes: {e}")
        input("\nPressione ENTER para sair...")
        return
    
    try:
        test_client = translate.TranslationServiceClient()
        print("  ✓ Translation API: Conectado")
    except Exception as e:
        print(f"  ✗ Translation API: ERRO")
        print(f"  Detalhes: {e}")
        print("\n  DICA: Verifique se a Translation API está HABILITADA no Google Cloud Console")
        input("\nPressione ENTER para sair...")
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
    print(f"  Modo DEBUG: {'SIM' if DEBUG_MODE else 'NÃO'}")
    
    try:
        device_index = int(os.getenv("INPUT_DEVICE_INDEX"))
        device_info = audio_system.get_device_info_by_index(device_index)
        print(f"  Dispositivo: [{device_index}] {device_info['name']}")
        
        if device_info['maxInputChannels'] == 0:
            print(f"\n  ⚠ AVISO: Este dispositivo não tem canais de entrada!")
            print(f"  Execute 'python teste_microfone.py' para listar dispositivos corretos")
            input("Pressione ENTER para continuar mesmo assim...")
    except Exception as e:
        print(f"  Dispositivo: Padrão do sistema (erro ao identificar: {e})")
        device_index = None
    
    stream = audio_system.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=CHUNK
    )
    
    # Validação do stream de áudio
    print(f"\n[VALIDAÇÃO] Testando captura de áudio...")
    try:
        # Tenta ler um chunk de teste
        test_data = stream.read(CHUNK, exception_on_overflow=False)
        
        # Processa conforme número de canais
        test_samples = np.frombuffer(test_data, dtype=np.int16)
        
        if CHANNELS == 2:
            # Garante que temos dados suficientes
            if len(test_samples) >= 2:
                num_frames = len(test_samples) // 2
                test_samples = test_samples[:num_frames * 2]
                test_samples = test_samples.reshape(-1, 2)
                test_samples_mono = test_samples.mean(axis=1).astype(np.int16)
            else:
                test_samples_mono = np.array([], dtype=np.int16)
        else:
            test_samples_mono = test_samples
        
        if len(test_samples_mono) > 0:
            test_rms = np.sqrt(np.mean(test_samples_mono.astype(np.float32)**2)) / 32768.0
            
            print(f"  ✓ Captura funcionando")
            print(f"  ✓ RMS inicial: {test_rms:.4f} (limiar: {SILENCE_THRESHOLD})")
            
            if test_rms < SILENCE_THRESHOLD:
                print(f"  ⚠ AVISO: Áudio muito baixo!")
                print(f"    Se não captar som, aumente o volume ou reduza SILENCE_THRESHOLD")
                print(f"    Recomendação: SILENCE_THRESHOLD={test_rms * 0.5:.4f}")
        else:
            print(f"  ⚠ AVISO: Não foi possível calcular RMS (sem dados)")
        
    except Exception as e:
        print(f"  ✗ ERRO na captura: {type(e).__name__}: {e}")
        print(f"\n  Tentando continuar mesmo assim...")

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