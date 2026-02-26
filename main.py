"""
Entry point for Render.com deployment with webhook support.
"""

import os
import logging
import asyncio
from threading import Thread
from flask import Flask, request
from telegram import Update
from bot import create_application, BOT_TOKEN, WEBHOOK_URL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)

# Create Telegram bot application
telegram_app = create_application()

# Global event loop for async operations
loop = None


def run_async(coro):
    """Run coroutine in the background event loop."""
    global loop
    if loop is None or loop.is_closed():
        # Create new event loop if needed
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=180)


def start_event_loop():
    """Start event loop in background thread."""
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever()


@app.route("/")
def home():
    return "✅ Instagram Bot is alive!", 200


@app.route("/health")
def health():
    return {"status": "ok", "bot_configured": bool(BOT_TOKEN and WEBHOOK_URL)}, 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Handle incoming Telegram updates via webhook."""
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        run_async(telegram_app.process_update(update))
        return "ok", 200
    except Exception as e:
        logger.error(f"Error processing update: {e}", exc_info=True)
        return "error", 500


@app.route("/set_webhook")
def set_webhook():
    """
    Set the webhook URL for the bot.
    Visit this endpoint once after deployment: https://your-app.onrender.com/set_webhook
    """
    if not WEBHOOK_URL:
        return "❌ WEBHOOK_URL environment variable is not set!", 400
    
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        
        async def _set_webhook():
            await telegram_app.bot.set_webhook(
                url=webhook_url,
                allowed_updates=["message"]
            )
            return await telegram_app.bot.get_webhook_info()
        
        webhook_info = run_async(_set_webhook())
        
        return {
            "status": "success",
            "webhook_url": webhook_url,
            "webhook_info": {
                "url": webhook_info.url,
                "pending_update_count": webhook_info.pending_update_count,
                "last_error_message": webhook_info.last_error_message
            }
        }, 200
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}, 500


@app.route("/delete_webhook")
def delete_webhook():
    """Delete the webhook (useful for debugging)."""
    try:
        run_async(telegram_app.bot.delete_webhook())
        return {"status": "webhook deleted"}, 200
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}, 500


@app.route("/webhook_info")
def webhook_info():
    """Get current webhook information."""
    try:
        info = run_async(telegram_app.bot.get_webhook_info())
        
        return {
            "url": info.url,
            "has_custom_certificate": info.has_custom_certificate,
            "pending_update_count": info.pending_update_count,
            "last_error_date": info.last_error_date,
            "last_error_message": info.last_error_message,
            "max_connections": info.max_connections,
            "allowed_updates": info.allowed_updates
        }, 200
    except Exception as e:
        logger.error(f"Failed to get webhook info: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}, 500


def initialize_bot():
    """Initialize the bot on startup."""
    try:
        async def _init():
            await telegram_app.initialize()
            await telegram_app.start()
        
        run_async(_init())
        logger.info("Bot initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")
    
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set. Remember to set it in Render environment variables!")
    
    # Start background event loop
    logger.info("Starting background event loop...")
    loop_thread = Thread(target=start_event_loop, daemon=True)
    loop_thread.start()
    
    # Wait for loop to start
    import time
    time.sleep(0.5)
    
    # Initialize bot
    initialize_bot()
    
    # Start Flask server
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask server on port {port}")
    if WEBHOOK_URL:
        logger.info(f"After deployment, visit {WEBHOOK_URL}/set_webhook to activate the bot")
    
    app.run(host="0.0.0.0", port=port, debug=False)
