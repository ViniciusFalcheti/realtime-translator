import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Configurações do ProPresenter
PROPRESENTER_IP = os.getenv("PROPRESENTER_IP")
PROPRESENTER_PORT = int(os.getenv("PROPRESENTER_PORT"))
PASSWORD = os.getenv("PROPRESENTER_PASSWORD")

def send_text_to_presenter(text):
    """
    Envia o texto para a camada de legendas (Props) do ProPresenter 7.
    """
    # URL CORRIGIDA
    url = f"http://{PROPRESENTER_IP}:{PROPRESENTER_PORT}/v1/stage/message"

    payload = text
    try:
        response = requests.put(
            url,
            json=payload,                # ← enviar como JSON
            auth=("pro", PASSWORD),
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        # response = requests.post(url, data=json.dumps(payload), headers=headers)
        response.raise_for_status()
        # print("Texto enviado com sucesso para o ProPresenter!")
        return True
    except requests.exceptions.HTTPError as errh:
        print(f"Erro HTTP: {errh.response.status_code} - {errh.response.reason}")
        print(f"Resposta do Servidor: {errh.response.text}")
    except requests.exceptions.ConnectionError as errc:
        print(f"Erro de Conexão: Verifique o IP e a porta do ProPresenter. {errc}")
    except requests.exceptions.RequestException as err:
        print(f"Ocorreu um erro inesperado: {err}")
    
    return False

if __name__ == '__main__':
    # Exemplo de uso:
    # test_text = "Esta é uma mensagem de teste para o ProPresenter2."
    # send_text_to_presenter(test_text)
    pass