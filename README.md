# Aleph

Aleph is a personal Telegram agent. The conversation lives in the bot. The Mini App is a separate plan and settings view, with the dark Void theme, a glass tab bar and Russian and English interfaces. Hermes Agent runs the conversational tool loop.

- Hermes can choose a Telegram HTML widget for a checklist, tabbed comparison or chart. Widgets render and respond to taps inside the message. Checklist marks are saved to the original widget. If Telegram rejects inline HTML, Aleph sends an interactive card with buttons in the chat and offers the same HTML widget through a signed Mini App view.
- Detailed Telegram articles can include photos from Wikimedia Commons with author, source and license credits. Aleph searches when an image helps explain the topic and delivers the article even if no photo is available.
- Create projects with their own instructions, reference text, conversation history, tasks, schedules and saved facts. The Mini App shows which project is active in chat. Use “создай проект Пилот”, “переключись на проект Пилот” or “выйди из проекта” in chat. You can paste notes or add a text file to the project context in the Mini App. Existing user history and tasks belong to the Personal workspace.
- Expand project folders in **Tasks** to see their tasks and completed work. Use **Save to project** under a detailed chat answer or delivered report to keep it in the project's **Results**.
- Add articles, YouTube videos, Notion pages and Google Drive files under **Projects → Sources**. Upload PDFs, documents, spreadsheets, presentations or text up to 4 MB. Aleph searches the saved content for relevant passages in the project's chat and scheduled reports. Links can be refreshed. Use Jina Reader from a saved public article when a direct read is unavailable. YouTube accepts an attached TXT, SRT or VTT transcript when captions are unavailable. Private Notion and Drive sources use the account's permissions.
- Create reusable skills in Settings → Skills. Import the text of a `SKILL.md` from a file, a public GitHub link to the file, or its GitHub folder. Review the instructions before saving, then enable the skill from the project's Skills panel. Aleph reads enabled skill instructions on demand in chat and scheduled reports, using the tools available for that request.
- Send text, voice, a photo or an office document. Aleph can read attachments, create files from scratch and return edited or converted copies in Telegram.
- Ask it to remember a preference, recall it across conversations, or forget it. A recent conversation window and explicitly saved memories are stored separately.
- Ask Aleph to prepare and send a result later. For example, “Tomorrow at 18:00, send me an analysis of SpaceX.” Aleph starts the work at the scheduled time and sends the result in Telegram. You can open the task in the Mini App to change the time or pause it.
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

The Telegram server runs on Python 3.12. Hermes runs in a separate Python 3.14 environment. Install uv and run `deploy/install-hermes.sh` once. Use Node.js 24 or newer to build the Mini App and native widget scripts before starting the server. Set ALEPH_HERMES_PYTHON if you installed Hermes to another directory. Set TELEGRAM_BOT_TOKEN and OPENCODE_API_KEY, or set TELEGRAM_BOT_TOKEN_FILE and OPENCODE_API_KEY_FILE to private files. Configure MINI_APP_URL with a publicly reachable HTTPS URL proxying to the service. An optional MINI_APP_URL_FILE supplies an updated URL on temporary installations.

    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements-voice.txt
    python deploy/install-bluetts.py
    npm ci
    npm run build
    python -m aleph

Hermes is pinned to upstream v0.23.2. Each turn runs with a private temporary Hermes home. It receives just the selected project history and an authenticated loopback connection to the Aleph tool and provider broker. Aleph owns the SQLite checkpoints and model credentials. `ALEPH_ENGINE=direct` selects the previous engine for operator rollback.

The server listens on 127.0.0.1:8787 by default. Set BIND and PORT to change this. ALEPH_DB selects the SQLite file. WHISPER_PYTHON may point to a separate interpreter with faster-whisper installed. The model client uses OpenCode Go, requests high reasoning effort, and supplies a stable, opaque session identifier per conversation. Claude Haiku 5.5 is the default for chat and photos. Set OPENCODE_MODEL and optionally OPENCODE_VISION_MODEL to compatible models available under your account’s regional settings. The Cloud instance uses OPENCODE_FALLBACK_MODEL=gpt-6-luna if OpenCode rejects the primary model for the account’s region. It retries the primary model after 30 minutes.

## Mini App and delivery

The main menu opens the live API-backed app. The API authenticates Telegram initData, rejects expired or invalid signatures, and scopes reads and writes to the signed account. Opening the web address in an ordinary browser shows a local demo. The frontend build contains no service credentials.

Frontend logic lives in `frontend/` and uses strict TypeScript. The build combines it with HTML, styles and assets from `web/` and emits `dist/web/`. Docker builds these files in a dedicated stage. Rebuild and restart the Python process after changing the native widget script.

Static hosting is optional. After enabling GitHub Pages (GitHub Actions source) in repository settings, manually run the optional Pages workflow. It builds and publishes `dist/web/`. With MINI_APP_BRIDGE_URL set to the resulting HTTPS base, the bot can also open the static app using a Telegram keyboard button. A size-limited snapshot of the plan travels in the private button URL fragment. Changes from that view return to the bot through Telegram web_app_data and are validated using the sender’s account. The API-backed menu offers the full plan.

Telegram delivery queues and tool results are persisted before replies. A crash exactly after Telegram accepts a message but before its acknowledgement is saved can result in duplicate delivery. Scheduler notifications are delivered while the service is online. An active webhook on the token must be removed by the operator before running this polling implementation.

## Connect ChatGPT on a self-hosted instance

Aleph can use eligible ChatGPT Plus or Pro plan usage for the owner of a personal, self-hosted instance. Set `ALEPH_CHATGPT_OWNER_ID` to the owner's numeric Telegram user ID and restart Aleph. This follows the [OpenAI self-hosted VM flow](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms). An app that offers ChatGPT plan usage as a shared hosted service needs [OpenAI hosted-app access](https://developers.openai.com/siwc/token-sharing-open-source).

1. On the computer where you plan to sign in, download Aleph and install its Python dependencies. If you already have the repository, start inside the directory where you cloned it.

   ```sh
   git clone https://github.com/ignatnovichenko/Aleph.git
   cd Aleph
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

2. Open **Settings → Connections → ChatGPT** in the live Telegram Mini App and create a one-time code. Then run the helper from the cloned repository.

   ```sh
   .venv/bin/python -m aleph.chatgpt_login connect
   ```

3. Paste the setup code into the helper's hidden prompt. Sign in on the OpenAI page in your system browser, then allow Aleph to use your ChatGPT plan. Return to the Mini App to see the connected account and model.

On Windows, use `py -m venv .venv` and `.venv\Scripts\python.exe` in place of `.venv/bin/python`. If Python reports `No module named 'aleph'`, check that your terminal is in the cloned repository before running the helper.

The helper completes OAuth locally and transfers the verified session to the instance in an encrypted packet. The code is valid for ten minutes and can be used once. Credentials and refresh tokens stay in the server's private directory. Set `ALEPH_CHATGPT_DIR` to select another directory. The Docker deployment keeps its session under `/data/chatgpt`. Settings can disconnect the session and link to [ChatGPT usage](https://chatgpt.com/settings/usage). Other Telegram users on the instance use OpenCode. When the owner's ChatGPT connection fails, Aleph shows the provider failure and offers reconnection or usage management.

Once connected, the server refreshes the ChatGPT session as needed. Updates and restarts preserve the connection while the credential directory is preserved. Renew the connection after revoking access in ChatGPT, losing the session file or moving to a server without it.

In **Settings → Chat replies**, the owner can choose ChatGPT or OpenCode, then select a model and a reasoning level from that provider's catalog. Aleph remembers the last selection for each provider in SQLite and shows the model used for its most recent model reply. Preferences apply to requests that start after saving.

## Run a project on your computer

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) on your computer. Use macOS, Linux or a Linux terminal in Windows Subsystem for Linux.
2. Open a terminal on that computer, clone `https://github.com/ignatnovichenko/Aleph.git` with `git clone` and run `cd Aleph`. Pull the latest version if you already have the repository.
3. In the Telegram Mini App, open **Settings → Computers → Connect a computer** and name the computer. Copy the command shown there and run it in the Aleph folder. The setup script installs the pinned Hermes runtime on first use. Paste the one-time code when the terminal asks for it.
4. In a project, open **Runs on** and select that computer. Keep the runner open while Aleph works. On the same computer, start it again with `sh deploy/computer.sh run`.

The runner connects over HTTPS without an incoming port. Hermes runs on the selected computer and receives that project's turn context. Your selected model, Telegram delivery and integrations are brokered by the Aleph server. Connected computers keep provider credentials on the server. Each optional project folder requires a local access grant. When a computer is offline, scheduled work waits for it to reconnect. To switch back, select **Aleph server** for the project. Disconnecting a computer in Settings revokes its credentials and moves its projects back to the server.

### Chrome on the paired computer

Install desktop Google Chrome and start the updated runner in a graphical desktop session. Existing pairings keep their token. In a private Telegram chat, try “В Chrome открой https://example.org и найди форму”. Aleph can use multiple tabs, click visible controls, fill forms and save downloads to `~/.config/aleph/browser/downloads`. Browser tasks start only with an online runner. The selected model receives visible page text and control labels. The dedicated profile, site sessions and password fields stay on the computer.

For manual sign-in, run these commands from the Aleph checkout. Close the profile window after signing in.

```sh
sh deploy/computer.sh browser-stop
sh deploy/computer.sh browser-login
sh deploy/computer.sh browser-resume
```

**Emergency stop.** Tap **Стоп** on the Telegram task or send `/browser_stop`. The server blocks new steps immediately upon receipt. An online runner closes its automation window and acknowledges shutdown. If Telegram or the network is unavailable, run `sh deploy/computer.sh browser-stop` locally. A loss of contact with the server also interrupts the active run. An already submitted transaction cannot be recalled. To accept fresh tasks after stopping, explicitly unlock in the same place (`/browser_resume` or `browser-resume`) and send a new instruction. Old tasks never resume.

Before a consequential action, Telegram shows the site, action and available form details. Approval applies once. To authorize that precise operation in a new task in advance, add a separate line beginning with **Разрешаю:** followed by the exact action description. Changed details require fresh approval. Passwords, payment card data and verification codes should be entered manually using the dedicated profile.

## Document workflows in Telegram

Attach the file and describe the change in its caption. To continue editing a file Aleph sent back, reply to the attachment. To combine PDF files, attach them together and say how to arrange their pages. Files are scoped to the current workspace.

- Word and presentations support exact text replacements across formatted runs. Spreadsheets support typed cell changes and standard Excel formulas. Aleph can build new documents, workbooks and presentations from a chat request.
- PDF supports page extraction, merging, removal, reordering, rotation, interactive form filling and text replacements. Changing the text in a PDF can reflow its layout. OCR reads English and Russian scans in batches of up to six image-only pages. Choose specific pages for longer scans.
- Chat supports PDF, DOCX, XLSX, PPTX, ODT, ODS, ODP, RTF, TXT, Markdown, CSV, TSV, JSON, HTML and ICS. Legacy DOC, XLS and PPT and layout-preserving Office conversions use LibreOffice inside Bubblewrap. On servers without the sandbox, modern formats use the built-in parsers and exporters.
- Chat supports files up to 16 MB and a combined input of 24 MB per operation. PDF operations support up to 250 pages.

For a bare-metal server, install DejaVu Sans, Poppler, Tesseract with English and Russian data, Bubblewrap and LibreOffice. Set ALEPH_TESSDATA to a custom language data directory if needed. ALEPH_OFFICE_BIN can select a LibreOffice installation outside the system path. The server must allow Bubblewrap user namespaces for native conversions.

## Checks

    npm ci
    npm run build
    python -m unittest discover -s tests -v

GET /healthz checks the HTTP process and SQLite. The Mini App API returns 401 without Telegram authentication. Data handling and the chat-based erasure procedure are described in [the privacy notice](web/privacy.html).

## Calendar, Notion, Drive and Gmail

Set an HTTPS origin under your control as `ALEPH_INTEGRATIONS_ORIGIN`, for example `https://aleph.example.com`. Every callback must use that exact origin. Set `ALEPH_BOT_USERNAME` to your bot’s username for the return link shown in the browser. Generate the encryption key with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and put it in `ALEPH_INTEGRATIONS_KEY`. Keep this key with your private backups. Changing it requires reconnecting existing accounts. A bare-metal deployment can use the `_FILE` variant for each client secret and the encryption key. In Compose use a private `.env` file, never commit it.

| Service | Setup | What Aleph can do |
| --- | --- | --- |
| Google Calendar | Enable Calendar API. Register `/oauth/calendar/callback` on a Google Web OAuth client. Set `ALEPH_GOOGLE_CLIENT_ID` and `ALEPH_GOOGLE_CLIENT_SECRET`. | Read the primary calendar and the Aleph calendar. Create events after confirmation in Telegram. |
| Notion | Create a Notion OAuth integration. Enable read and insert capabilities, register `/oauth/notion/callback`, then set `ALEPH_NOTION_CLIENT_ID` and `ALEPH_NOTION_CLIENT_SECRET`. | Search and read granted pages. Append approved notes. Paste a Notion page link into a project material to make it available as project context. |
| Google Drive | Enable Drive and Picker APIs. Register `/oauth/drive/callback`. Make a browser-restricted Picker key and configure `ALEPH_GOOGLE_PICKER_KEY` and `ALEPH_GOOGLE_PROJECT_NUMBER`. | Read Google Docs and selected text formats. Create approved Google Docs. The file picker grants per-file access. |
| Gmail | Enable Gmail API on the Google project and register `/oauth/gmail/callback`. | Search and read messages on request. Save approved drafts to Gmail. |

In Google Cloud, add your accounts as testers to exercise Calendar, Drive and Gmail while the app is in Testing. Google testing tokens for these scopes expire after seven days. Review the Google verification requirements before publishing, especially Gmail’s restricted scopes. Notion public OAuth allows other workspaces to connect after setting up the integration as public. Each Telegram user grants their own access in Settings → Connections. Disconnect removes the app’s stored credentials. Provider account settings control revocation of the wider OAuth grant.

The app uses structured Telegram Rich Messages for longer Markdown responses. If Telegram rejects the formatting, the bot sends the answer as ordinary messages. Task deadlines and reminders have separate fields. Existing timed tasks keep their original reminder times. A reminder can be completed or postponed from Telegram.

## Project folders and proactive notifications

Pair a computer and assign it to the project. The project’s computer sheet shows its project number. On the paired computer run `sh deploy/computer.sh folder --project 12 --path "/path/to/Offers"` with the displayed number and chosen directory. Omit `--path` to revoke access. The next turn sees the new grant. The runner exposes supported documents in that directory on request. Opening a document transfers its contents to the Aleph server for processing and selected excerpts to the configured model. Returned files are saved alongside the originals, with a distinct name if necessary. Chat documents and prior versions are accessible in Projects → Results.

Settings → Aleph’s initiative controls suggestions, focus, and use of connected sources. Explicit conditions work on all three levels. A new user can describe priorities during onboarding. With source personalization enabled, selected project Notion and Drive links refresh in the background. Fixed daily briefs are optional.

For event-driven Gmail alerts, set up the Gmail OAuth integration above. In the same Google Cloud project as the OAuth client, enable Pub/Sub, create a topic, and grant `gmail-api-push@system.gserviceaccount.com` the Pub/Sub Publisher role on that topic. Set `ALEPH_GMAIL_TOPIC` to its full resource name. Generate a random URL-safe secret of at least 32 characters and set `ALEPH_GMAIL_PUSH_SECRET`. Create a Pub/Sub push subscription on the topic with the HTTPS endpoint `/events/gmail?token=<secret>` on your stable Aleph origin. Protect that URL as a credential and omit query strings from access logs. The server validates incoming notifications and reads matching new messages through the mailbox owner's OAuth session. Google can delay or drop push notifications. Closing a watch task pauses it. Reconnect Gmail and create new rules after changing accounts.

Ask “Tell me when an email from sales@example.org arrives with quotation in the subject.” For a price list, add a table to project sources and ask to watch one product and a percentage rise. A linked list refreshes about every 15 minutes. Upload a new local list with the same filename to compare again. Aleph compares a numeric column in an unambiguous product row.

### Working with Aleph in a Telegram group

Open Aleph privately and choose **Add to a group** under `/start`. In a group or forum topic, send `/ask@AlephAgentBot` followed by a request. Reply to a colleague’s message with the same command to include their text or Telegram’s selected quote. Reply to Aleph’s response to follow up. For a self-hosted copy, substitute its Telegram username.

Public questions and research use the server’s model and public reference sources. Results go back to the original message and topic. Personal accounts, project files and actions use the requester’s own private chat. Existing users receive the request there automatically. First-time users can open the owner-bound button on the group reply for 24 hours. A Notion or Drive write follows the normal private preview and approval flow and requires the corresponding OAuth connection.

Telegram supplies addressed slash commands and replies with group privacy enabled. For plain @mentions in ordinary group messages, the owner can use BotFather’s `/setprivacy` setting and re-add the bot to existing groups. Aleph discards unrelated conversation even when Telegram delivers it. [Telegram group privacy](https://core.telegram.org/bots/features#privacy-mode)


### On-demand voice replies

Ask “ответь голосовым” in a message or voice note. Aleph sends the answer using Telegram’s `sendVoice` in the requesting conversation, including the topic for a public group reply. Without that request the standard reply is text. Asking just “ответь голосовым” in a private chat reads the previous answer from the current project.

The Linux x86-64 installer uses the official BlueTTS v3.0.1 Russian frontend, the optimized FP32 graphs under `onnx/` on ONNX Runtime’s CPU provider, and FFmpeg for mono Ogg/Opus. For a native installation, install `uv`, `curl`, `git` and `ffmpeg` and run `python deploy/install-bluetts.py`. `ALEPH_BLUETTS_HOME` overrides the default `~/.local/share/aleph/bluetts` for installation and serving. The amd64 container includes this runtime. The stock David voice uses LibriTTS-R speaker 708 (CC BY 4.0, Koizumi et al., 2023). [Model and component licenses](https://huggingface.co/notmax123/BlueV3-onnx/blob/8410bbfe508a9b84a967324ecf3caf5f655326c9/LICENSES.md)
