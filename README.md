# ✦ ALI AI

Multimodal Telegram assistant by ZEEYO — streaming text answers, image/GIF vision, voice notes,
PDF/text summaries and image generation (Imagen 3).

- **Animated responses**: a rotating spark spinner with changing status phrases while thinking, then
  the answer streams in live with a cursor and is re-rendered as formatted Telegram HTML.
- **Logo**: `assets/logo.png` (static) and `assets/logo_spin.gif` (rotating), shown on `/start`.
  Regenerate with `pip install pillow && python tools/make_logo.py`.
- **Groups**: replies only when mentioned or replied to.

## Run
```
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and GEMINI_API_KEY
pip install -r requirements.txt
python bot.py
```
Set the bot's profile picture in @BotFather (`/setuserpic`) using `assets/logo.png`.

## Using ALI anywhere
One-time setup in @BotFather (select your bot):
1. `/setinline` → set a placeholder like `Ask ALI anything…` (enables `@username question` in **any** chat).
2. `/setinlinefeedback` → **Enabled** (makes the answer start automatically; without it, tap "✦ Get answer").
3. `/setjoingroups` → **Enabled** so it can be added to groups.
4. Channels: add the bot as an **admin**; a channel post that mentions `@username` gets an answer.

Groups: mention the bot or reply to its messages. Mentions work even with privacy mode on.
