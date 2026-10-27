import os
import time
import threading
import requests
from flask import Flask
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv

load_dotenv()

# إعداد خادم الويب الوهمي لإبقاء الخدمة مستيقظة على Render
app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Polygon Liquidation Bot is Running 24/7!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
BOT_CONTRACT_ADDRESS = os.getenv("BOT_CONTRACT_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not POLYGON_RPC_URL or not PRIVATE_KEY or not BOT_CONTRACT_ADDRESS:
    print("[-] Error: Missing required environment variables.")
    exit()

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not w3.is_connected():
    print("[-] Error: Failed to connect to Polygon Mainnet RPC.")
    exit()

print("[+] Successfully connected to Polygon Mainnet.")

account = w3.eth.account.from_key(PRIVATE_KEY)
bot_address = account.address
print(f"[+] Bot Wallet Address Loaded: {bot_address}")

PROTOCOLS = {
    "Aave_v3": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    "Radiant_v2": "0x265E9Dcf7C286eA35541e3E4F4256A5B20235334"
}

POOL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

BOT_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "lendingPoolAddress", "type": "address"},
            {"internalType": "address", "name": "collateralAsset", "type": "address"},
            {"internalType": "address", "name": "debtAsset", "type": "address"},
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "uint256", "name": "debtToCover", "type": "uint256"},
            {"internalType": "bool", "name": "receiveAToken", "type": "bool"}
        ],
        "name": "executeLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

bot_contract = w3.eth.contract(address=w3.to_checksum_address(BOT_CONTRACT_ADDRESS), abi=BOT_ABI)

tracked_borrowers = {
    "Aave_v3": {"0x8539f40398f6d507dd43dff63f533a1e3ff2bf83"},
    "Radiant_v2": set()
}

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[-] Telegram alert error: {e}")

def execute_onchain_liquidation(proto_name, pool_address, borrower):
    print(f"\n[🚀] TRIGGERING LIQUIDATION ON [{proto_name}] FOR: {borrower}")
    try:
        collateral_asset = "0x0d500B1d3E8eF31241C9B95d2d3E64b44161f805"  # WMATIC
        debt_asset = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"        # USDC
        debt_to_cover = 100 * 10**6  # 100 USDC
        
        nonce = w3.eth.get_transaction_count(bot_address)
        tx = bot_contract.functions.executeLiquidation(
            w3.to_checksum_address(pool_address),
            w3.to_checksum_address(collateral_asset),
            w3.to_checksum_address(debt_asset),
            w3.to_checksum_address(borrower),
            debt_to_cover,
            False
        ).build_transaction({
            'from': bot_address,
            'nonce': nonce,
            'gas': 600000,
            'maxFeePerGas': w3.to_wei('100', 'gwei'),
            'maxPriorityFeePerGas': w3.to_wei('35', 'gwei'),
            'chainId': 137
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = w3.to_hex(tx_hash)
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt['status'] == 1:
            success_msg = f"✅ *Liquidation Executed!*\n🌐 Protocol: `{proto_name}`\n👤 Borrower: `{borrower}`\n🔗 Tx: `{tx_hash_hex}`"
            send_telegram_alert(success_msg)
        else:
            send_telegram_alert(f"❌ *Liquidation Reverted!*\n🌐 Protocol: `{proto_name}`")
    except Exception as e:
        send_telegram_alert(f"⚠️ *Liquidation Error!*\n🌐 Protocol: `{proto_name}`\n❌ Error: `{str(e)}`")

def scan_liquidations():
    while True:
        for proto_name, pool_addr in PROTOCOLS.items():
            borrowers = tracked_borrowers.get(proto_name, set())
            if not borrowers:
                continue
            pool_contract = w3.eth.contract(address=w3.to_checksum_address(pool_addr), abi=POOL_ABI)
            for borrower in list(borrowers):
                try:
                    data = pool_contract.functions.getUserAccountData(borrower).call()
                    health_factor = data[5]
                    if 0 < health_factor < 10**18:
                        execute_onchain_liquidation(proto_name, pool_addr, borrower)
                except Exception:
                    pass
        time.sleep(15)

if __name__ == "__main__":
    print("[*] Starting Web Server & Liquidation Scanner Thread...")
    send_telegram_alert("🤖 *Liquidation Bot (Web Service) Started!* Ready on Render.")
    
    # تشغيل الماسح في خلفية مستقلة (Background Thread)
    scanner_thread = threading.Thread(target=scan_liquidations, daemon=True)
    scanner_thread.start()
    
    # تشغيل خادم الويب الأساسي للامتثال لشروط Render المجانية
    run_web_server()
