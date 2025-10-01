import pyaudio

p = pyaudio.PyAudio()
info = p.get_host_api_info_by_index(0)
numdevices = info.get('deviceCount')

print("--- Dispositivos de Áudio Encontrados ---")

for i in range(0, numdevices):
    device_info = p.get_device_info_by_host_api_device_index(0, i)

    # Filtra apenas dispositivos de entrada (microfones)
    if (device_info.get('maxInputChannels')) > 0:
        print(f"Index: {i} - Nome: {device_info.get('name')} - Taxa: {device_info.get('defaultSampleRate')}")

p.terminate()