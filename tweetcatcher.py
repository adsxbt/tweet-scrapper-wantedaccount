import websocket
import json
import time
import ssl
import requests
from datetime import datetime
import discord
from discord.ext import commands
import asyncio
import os
from urllib.parse import urlparse
import logging
from dotenv import load_dotenv

# Chargement des variables d'environnement
load_dotenv()

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('tweetcatcher.log'),
        logging.StreamHandler()
    ]
)

# Configuration
WEBSOCKET_URL = 'wss://pumpportal.fun/api/data'
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
ALLOWED_ACCOUNTS_FILE = 'allowed_accounts.txt'
TWEET_BLACKLIST_FILE = 'tweet_blacklist.txt'
REQUEST_TIMEOUT = 10

# Vérification des variables d'environnement
if not DISCORD_TOKEN or not CHANNEL_ID:
    logging.error("Variables d'environnement manquantes. Vérifiez votre fichier .env")
    exit(1)

# Initialisation des listes
tweet_blacklist = []
allowed_accounts = []

def load_allowed_accounts():
    """Charge la liste des comptes autorisés"""
    try:
        with open(ALLOWED_ACCOUNTS_FILE, 'r') as f:
            return [line.strip().lower() for line in f if line.strip()]
    except FileNotFoundError:
        logging.error(f"Fichier {ALLOWED_ACCOUNTS_FILE} non trouvé")
        return []

def load_tweet_blacklist():
    """Charge la liste des tweets déjà traités"""
    try:
        with open(TWEET_BLACKLIST_FILE, 'r') as f:
            return [line.strip() for line in f if line.strip() and not line.startswith('#')]
    except FileNotFoundError:
        logging.error(f"Fichier {TWEET_BLACKLIST_FILE} non trouvé")
        return []

def save_to_blacklist(tweet_id, account_name):
    """Sauvegarde un tweet dans la blacklist"""
    try:
        with open(TWEET_BLACKLIST_FILE, 'a') as f:
            f.write(f"{tweet_id}|{account_name}\n")
        tweet_blacklist.append(f"{tweet_id}|{account_name}")
    except Exception as e:
        logging.error(f"Erreur lors de la sauvegarde dans la blacklist: {e}")

def extract_account_name(twitter_link):
    """Extrait le nom du compte depuis l'URL Twitter"""
    try:
        if '#' in twitter_link:
            twitter_link = twitter_link.split('#')[0]
        
        if '/status/' in twitter_link:
            twitter_link = twitter_link.split('/status/')[0]
            
        parts = twitter_link.split('/')
        for i, part in enumerate(parts):
            if part in ['x.com', 'twitter.com'] and i + 1 < len(parts):
                account_name = parts[i + 1].lower()
                # Si le nom du compte contient des caractères spéciaux, on le nettoie
                account_name = ''.join(c for c in account_name if c.isalnum() or c == '_')
                return account_name
        return None
    except Exception as e:
        logging.error(f"Erreur lors de l'extraction du nom du compte: {e}")
        return None

def extract_tweet_id(twitter_link):
    """Extrait l'ID du tweet depuis l'URL Twitter"""
    try:
        if '/status/' in twitter_link:
            return twitter_link.split('/status/')[1].split('#')[0].split('?')[0]
        return None
    except Exception as e:
        logging.error(f"Erreur lors de l'extraction de l'ID du tweet: {e}")
        return None

def get_token_links(uri):
    """Récupère les liens depuis les métadonnées du token"""
    links = {
        'twitter': 'Non disponible',
        'website': 'Non disponible',
        'telegram': 'Non disponible'
    }
    
    try:
        if not uri:
            return links
            
        response = requests.get(uri, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            metadata = response.json()
            
            # Recherche dans les propriétés
            if 'properties' in metadata and 'links' in metadata['properties']:
                links_data = metadata['properties']['links']
                links.update({
                    'twitter': links_data.get('twitter', links['twitter']),
                    'website': links_data.get('website', links['website']),
                    'telegram': links_data.get('telegram', links['telegram'])
                })
            
            # Recherche directe dans les métadonnées
            links.update({
                'twitter': metadata.get('twitter', metadata.get('twitter_link', links['twitter'])),
                'website': metadata.get('website', metadata.get('website_link', links['website'])),
                'telegram': metadata.get('telegram', metadata.get('telegram_link', links['telegram']))
            })
    except requests.Timeout:
        logging.warning(f"Timeout lors de la récupération des métadonnées: {uri}")
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des métadonnées: {e}")
    
    return links

class TweetCatcherBot(commands.Bot):
    def __init__(self):
        # Configuration des intents de base uniquement
        intents = discord.Intents.default()
        super().__init__(command_prefix='!', intents=intents)
        
    async def setup_hook(self):
        """Configuration initiale du bot"""
        self.channel = None
        self.websocket_thread = None
        
    async def on_ready(self):
        """Événement déclenché quand le bot est prêt"""
        logging.info(f'Bot connecté en tant que {self.user.name}')
        
        # Afficher les serveurs disponibles
        for guild in self.guilds:
            logging.info(f"Serveur trouvé: {guild.name} (ID: {guild.id})")
            # Afficher les canaux disponibles
            for channel in guild.channels:
                logging.info(f"- Canal: {channel.name} (ID: {channel.id})")
        
        # Récupération du canal
        self.channel = self.get_channel(CHANNEL_ID)
        if not self.channel:
            logging.error(f"Canal Discord {CHANNEL_ID} non trouvé")
            logging.error("Vérifiez que:")
            logging.error("1. Le bot est bien dans le serveur")
            logging.error("2. L'ID du canal est correct")
            logging.error("3. Le bot a accès au canal")
            return
            
        logging.info(f"Canal trouvé: {self.channel.name} dans {self.channel.guild.name}")
            
        # Démarrage du WebSocket
        try:
            import threading
            self.websocket_thread = threading.Thread(target=self.run_websocket, daemon=True)
            self.websocket_thread.start()
            logging.info("WebSocket thread démarré")
        except Exception as e:
            logging.error(f"Erreur lors du démarrage du WebSocket: {e}")
        
    async def send_tweet(self, twitter_link, account_name):
        """Envoie un tweet dans le canal Discord"""
        if not self.channel:
            logging.error("Canal Discord non initialisé")
            return
            
        try:
            await self.channel.send(f"🔔 Nouveau tweet de @{account_name}:\n{twitter_link}")
            logging.info(f"Tweet envoyé avec succès: {twitter_link}")
        except Exception as e:
            logging.error(f"Erreur lors de l'envoi du message Discord: {e}")

    def process_token(self, token_data):
        """Traite un nouveau token"""
        try:
            uri = token_data.get('uri', token_data.get('metadataUri', ''))
            logging.info(f"Traitement du token avec URI: {uri}")
            
            links = get_token_links(uri)
            twitter_link = links['twitter']

            if twitter_link == 'Non disponible':
                logging.info("Aucun lien Twitter trouvé dans les métadonnées")
                return

            account_name = extract_account_name(twitter_link)
            tweet_id = extract_tweet_id(twitter_link)

            if not account_name or not tweet_id:
                logging.info(f"Impossible d'extraire le nom du compte ou l'ID du tweet: {twitter_link}")
                return

            # Vérification de la blacklist
            tweet_key = f"{tweet_id}|{account_name}"
            if tweet_key in tweet_blacklist:
                logging.info(f"Tweet déjà traité: {tweet_key}")
                return

            # Vérification du compte autorisé
            if account_name not in allowed_accounts:
                logging.info(f"Compte non autorisé: {account_name}")
                return

            # Sauvegarde et envoi
            save_to_blacklist(tweet_id, account_name)
            logging.info(f"Nouveau tweet trouvé: {twitter_link}")
            asyncio.run_coroutine_threadsafe(
                self.send_tweet(twitter_link, account_name),
                self.loop
            )
            
        except Exception as e:
            logging.error(f"Erreur lors du traitement du token: {e}")

    def on_websocket_message(self, ws, message):
        """Gère les messages du WebSocket"""
        try:
            data = json.loads(message)
            logging.debug(f"Message WebSocket reçu: {data}")
            
            if 'method' in data and data['method'] == 'newToken':
                self.process_token(data['params'])
            elif 'type' in data and data['type'] == 'newToken':
                self.process_token(data)
            elif 'event' in data and data['event'] == 'token_created':
                self.process_token(data['data'])
            elif 'mint' in data and 'txType' in data and data['txType'] == 'create':
                self.process_token(data)
                
        except Exception as e:
            logging.error(f"Erreur lors du traitement du message WebSocket: {e}")

    def on_websocket_error(self, ws, error):
        """Gère les erreurs du WebSocket"""
        logging.error(f"Erreur WebSocket: {error}")

    def on_websocket_close(self, ws, close_status_code, close_msg):
        """Gère la fermeture du WebSocket"""
        logging.warning(f"WebSocket fermé: {close_status_code} - {close_msg}")

    def on_websocket_open(self, ws):
        """Gère l'ouverture du WebSocket"""
        logging.info("WebSocket connecté, en attente de nouveaux tweets...")
        ws.send(json.dumps({"method": "subscribeNewToken"}))
        logging.info("Souscription aux nouveaux tokens envoyée")

    def run_websocket(self):
        """Exécute la connexion WebSocket avec reconnexion automatique"""
        while True:
            try:
                ws = websocket.WebSocketApp(
                    WEBSOCKET_URL,
                    on_open=self.on_websocket_open,
                    on_message=self.on_websocket_message,
                    on_error=self.on_websocket_error,
                    on_close=self.on_websocket_close
                )
                ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception as e:
                logging.error(f"Erreur de connexion WebSocket: {e}")
            logging.info("Tentative de reconnexion WebSocket dans 10 secondes...")
            time.sleep(10)

def main():
    """Fonction principale"""
    # Chargement des listes
    global tweet_blacklist, allowed_accounts
    tweet_blacklist = load_tweet_blacklist()
    allowed_accounts = load_allowed_accounts()
    
    # Démarrage du bot
    bot = TweetCatcherBot()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    logging.info("Démarrage du TweetCatcher...")
    main()
