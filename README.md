# Aleph

A personal Telegram agent by Lab One. The conversation lives in the bot. The Mini App is a separate plan and settings view, with Lab One’s Void and Paper themes, a glass tab bar and Russian and English interfaces. Hermes Agent runs the conversational tool loop.

- Hermes can choose a Telegram HTML widget for a checklist, tabbed comparison or chart. Widgets render and respond to taps inside the message. Checklist marks are saved to the original widget. Telegram API delivery failures for unsupported widgets fall back to text.
- Create projects with their own instructions, reference text, conversation history, tasks, schedules and saved facts. The Mini App shows which project is active in chat. Use “создай проект Lab One”, “переключись на проект Lab One” or “выйди из проекта” in chat. You can paste notes or add a text file to the project context in the Mini App. Existing user history and tasks belong to the Personal workspace.
- Send text, voice, a photo, a PDF or a text document. The model can act on tasks, follow up on a schedule, and send TXT, Markdown, CSV and calendar files back to the chat.
- Ask it to remember a preference, recall it across conversations, or forget it. A recent conversation window and explicitly saved memories are stored separately.
- Set a one-off reminder or a recurring routine in plain language. Weather, RSS and agenda routines fetch fresh data when they run. The morning brief appears when there is something to report. Disable proactive briefs or set quiet hours in the Mini App. Explicit one-off reminders follow their requested times.
- Connect sources from the RSS catalog, look up reference material in Wikipedia, check Open-Meteo forecasts, or export scheduled tasks as ICS. ICS is a file export.
- Start with “Я в Москве. По будням в 08:30 присылай погоду в Москве” or “Разбери этот PDF и пришли список дел в Markdown”. The initial timezone is UTC. Tell the bot your timezone or select it in Settings.

## Run with a domain

Use one running instance per Telegram bot token. Stop any previous poller before starting another. Point your domain at a Linux host with Docker Compose and open ports 80 and 443. Create two separate secret files outside the repository, each holding one credential. They must be readable by container UID 10001. On Linux, store them in a private directory, assign owner 10001 and mode 0600. Set their **host paths** in a local copy of [.env.example](.env.example). Set ALEPH_DOMAIN to the public hostname.

    cp .env.example .env
    # Set ALEPH_DOMAIN and both secret-file paths in .env.
    docker compose up -d --build

Caddy provisions HTTPS. The application registers the Mini App in Telegram’s chat menu, polls Telegram, runs notifications, and keeps SQLite and downloaded speech models in a named volume. The first voice request needs access to Hugging Face to download the multilingual model. You can also mount a prepared faster-whisper model and set WHISPER_MODEL to its directory. Keep the data volume when rebuilding. Back up the live database with SQLite’s backup API.

## Python development

The Telegram server runs on Python 3.12. Hermes runs in a separate Python 3.14 environment. Install uv and run `deploy/install-hermes.sh` once, then start the server. Set ALEPH_HERMES_PYTHON if you installed Hermes to another directory. Set TELEGRAM_BOT_TOKEN and OPENCODE_API_KEY, or set TELEGRAM_BOT_TOKEN_FILE and OPENCODE_API_KEY_FILE to private files. Configure MINI_APP_URL with a publicly reachable HTTPS URL proxying to the service. An optional MINI_APP_URL_FILE supplies an updated URL on temporary installations.

    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements-voice.txt
    python -m aleph

Hermes is pinned to upstream v0.23.2. Each turn runs with a private temporary Hermes home. It receives just the selected project history and an authenticated loopback connection to the Aleph tool and provider broker. Aleph owns the SQLite checkpoints and model credentials. `ALEPH_ENGINE=direct` selects the previous engine for operator rollback.

The server listens on 127.0.0.1:8787 by default. Set BIND and PORT to change this. ALEPH_DB selects the SQLite file. WHISPER_PYTHON may point to a separate interpreter with faster-whisper installed. The model client uses OpenCode Go, requests high reasoning effort, and supplies a stable, opaque session identifier per conversation. The default model is deepseek-v4.1-flash. Set OPENCODE_MODEL and optionally OPENCODE_VISION_MODEL to compatible models available under your account’s regional settings. The Cloud instance keeps DeepSeek as its first choice and uses OPENCODE_FALLBACK_MODEL=gpt-6-luna when the provider rejects DeepSeek for this account’s region. It retries the primary model after 30 minutes.

## Mini App and delivery

The main menu opens the live API-backed app. The API authenticates Telegram initData, rejects expired or invalid signatures, and scopes reads and writes to the signed account. Opening the web address in an ordinary browser shows a local demo. The frontend build contains no service credentials.

Static hosting is optional. After enabling GitHub Pages (GitHub Actions source) in repository settings, manually run the optional Pages workflow. It publishes only web/. With MINI_APP_BRIDGE_URL set to the resulting HTTPS base, the bot can also open the static app using a Telegram keyboard button. A size-limited snapshot of the plan travels in the private button URL fragment. Changes from that view return to the bot through Telegram web_app_data and are validated using the sender’s account. The API-backed menu offers the full plan.

Telegram delivery queues and tool results are persisted before replies. A crash exactly after Telegram accepts a message but before its acknowledgement is saved can result in duplicate delivery. Scheduler notifications are delivered while the service is online. An active webhook on the token must be removed by the operator before running this polling implementation.

## Checks

    python -m unittest discover -s tests -v

GET /healthz checks the HTTP process and SQLite. The Mini App API returns 401 without Telegram authentication. Data handling and the chat-based erasure procedure are described in [the privacy notice](web/privacy.html).
