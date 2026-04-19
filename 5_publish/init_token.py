import os

import requests

# requete dans navigateur : 
# https://soundcloud.com/connect?client_id=CLIENT_ID&response_type=code&redirect_uri=http://localhost
# Valider l'accès dans le navigateur
# Copier le code dans l'URL de type : http://localhost/?code=...
# Puis exécuter ce script pour obtenir les tokens d'accès et de rafraîchissement

TOKEN_ENDPOINT = "https://api.soundcloud.com/oauth2/token"

url = os.getenv("TOKEN_ENDPOINT", TOKEN_ENDPOINT)

data = {
    'client_id': os.getenv("SOUNDCLOUD_CLIENT_ID"),
    'client_secret': os.getenv("SOUNDCLOUD_CLIENT_SECRET"),
    'grant_type': 'authorization_code',
    'redirect_uri': 'http://localhost',
    'code': '<VOTRE_CODE_ICI>'  # Remplacez par le code obtenu depuis le navigateur
}

response = requests.post(url, data=data)
tokens = response.json()
print("Response:", response.status_code, response.text)
print("Access Token:", tokens.get('access_token'))
print("Refresh Token:", tokens.get('refresh_token'))






