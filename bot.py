import os
import json
import base64
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "Наташа готовые экспорт")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# === GOOGLE SHEETS ===
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    return sheet

def ensure_headers(sheet):
    headers = ["Бренд", "Модель", "Комплектация", "Цвет", "Цена завода (USD)", "Цена +5%", "Дата обновления"]
    first_row = sheet.row_values(1)
    if first_row != headers:
        sheet.insert_row(headers, 1)

def write_to_sheet(sheet, rows):
    today = datetime.now().strftime("%d.%m.%Y")
    data = []
    for row in rows:
        price = row.get("price", 0)
        try:
            price_num = float(str(price).replace(",", "").replace(" ", ""))
            price_plus = round(price_num * 1.05)
        except:
            price_num = price
            price_plus = ""
        data.append([
            row.get("brand", ""),
            row.get("model", ""),
            row.get("trim", ""),
            row.get("color", ""),
            price_num,
            price_plus,
            today
        ])
    sheet.append_rows(data, value_input_option="USER_ENTERED")
    return len(data)

# === CLAUDE VISION ===
def parse_price_image(image_bytes: bytes) -> list:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """Ты парсишь прайс-лист автомобилей. Извлеки все строки из таблицы.

Для каждого автомобиля верни JSON объект с полями:
- brand: бренд (например BYD, Geely, Changan, Toyota и т.д.)
- model: модель (например Yuan UP, Starship 7 и т.д.)
- trim: комплектация (описание версии, если есть)
- color: цвет (если указан, иначе пустая строка)
- price: цена в USD (только число, без символов. Используй колонку "indicative price" или "FOB horgos USD")

Верни ТОЛЬКО валидный JSON массив без лишнего текста, например:
[
  {"brand": "BYD", "model": "Yuan UP", "trim": "Intelligent Driving 401KM transcendence", "color": "White Gray", "price": 119800},
  ...
]

Если прайс на китайском — переведи бренд и модель на английский или оставь транслитерацию."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    )

    text = response.content[0].text.strip()
    # Убираем markdown блоки если есть
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    rows = json.loads(text)
    return rows

# === TELEGRAM HANDLERS ===
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📥 Получил картинку, обрабатываю прайс...")

    try:
        # Скачиваем фото
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        await msg.edit_text("🔍 Распознаю данные с помощью AI...")

        # Парсим через Claude
        rows = parse_price_image(bytes(image_bytes))

        if not rows:
            await msg.edit_text("❌ Не удалось найти данные в прайсе. Попробуй другую картинку.")
            return

        await msg.edit_text(f"📊 Найдено {len(rows)} позиций, записываю в таблицу...")

        # Пишем в Google Sheets
        sheet = get_sheet()
        ensure_headers(sheet)
        count = write_to_sheet(sheet, rows)

        await msg.edit_text(
            f"✅ Готово! Добавлено {count} позиций в таблицу.\n"
            f"📋 Вкладка: {SHEET_NAME}"
        )

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await msg.edit_text("❌ Ошибка при разборе данных. Попробуй ещё раз или пришли более чёткую картинку.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        msg = await update.message.reply_text("📥 Получил файл, обрабатываю...")
        try:
            file = await context.bot.get_file(doc.file_id)
            image_bytes = await file.download_as_bytearray()
            await msg.edit_text("🔍 Распознаю данные...")
            rows = parse_price_image(bytes(image_bytes))
            if not rows:
                await msg.edit_text("❌ Данные не найдены.")
                return
            sheet = get_sheet()
            ensure_headers(sheet)
            count = write_to_sheet(sheet, rows)
            await msg.edit_text(f"✅ Готово! Добавлено {count} позиций в таблицу.")
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {str(e)}")
    else:
        await update.message.reply_text("Пришли картинку прайса (фото или изображение).")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для обработки прайсов авто.\n\n"
        "📸 Пришли мне картинку прайса — я распознаю все позиции и запишу в Google таблицу.\n\n"
        "Поддерживаю прайсы на английском и китайском языке."
    )

# === MAIN ===
def main():
    from telegram.ext import CommandHandler
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
