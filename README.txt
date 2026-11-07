CHAT ANALYSER - QUICK START

1. Extract this ZIP into your Downloads folder.
2. Open the extracted folder named chat_analyser_package.
3. Rename .env.example to .env (Windows may hide the leading dot; ensure the final filename is .env, not .env.txt).
4. Open .env in Notepad and fill TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org.
5. Open PowerShell in this folder and run:

   pip install telethon python-telegram-bot groq aiohttp
   python app.py --generate-session

6. Follow the prompts for your phone number and Telegram login code. Copy the generated session string into Render's TELEGRAM_SESSION environment variable.
7. Add the remaining required environment variables in Render's Environment page. Do not upload your personal .env or session string to GitHub or share them.

Keep app.py and .env in the same folder when generating the session.
