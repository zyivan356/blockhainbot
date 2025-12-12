import sqlite3
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
    JobQueue
)
import re
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы состояний для ConversationHandler
ADD_SOURCE, DELETE_SOURCE, SET_RANGE_MIN, SET_RANGE_MAX, SET_TIMEZONE, SET_NOTIFICATION_MODE = range(6)
SOLANA_RPC_URL = "https://api.devnet.solana.com"  # SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
DB_PATH = "solana_tracker.db"

# ВАЖНО: ЗАМЕНИТЕ ЭТИ ЗНАЧЕНИЯ НА СВОИ РЕАЛЬНЫЕ ДАННЫЕ
ADMIN_USER_ID = 5974263434  # Убедитесь, что это ваш правильный ID
BOT_TOKEN = "8522864763:AAH_-etbbLa0BCXjI-asiBsj7iFAYwQhdZE"  # Убедитесь, что токен действителен


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Таблица адресов-источников
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sources (
        address TEXT PRIMARY KEY
    )
    ''')

    # Таблица обработанных транзакций
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS processed_txs (
        signature TEXT PRIMARY KEY,
        timestamp INTEGER
    )
    ''')

    # Таблица уведомленных кошельков
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS notified_wallets (
        wallet_address TEXT PRIMARY KEY
    )
    ''')

    # Таблица настроек
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')

    # Инициализация настроек по умолчанию
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_amount', '0.001')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('max_amount', '10')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('timezone', '5')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_all_transactions', 'true')")

    conn.commit()
    conn.close()


# Очистка тестовых данных
def clear_test_data():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM processed_txs")
    cursor.execute("DELETE FROM notified_wallets")
    conn.commit()
    conn.close()
    logger.info("✅ Тестовые данные очищены")


# Получение настроек из БД
def get_settings():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    settings = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return settings


# Обновление настроек в БД
def update_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


# Работа с адресами-источниками
def add_source_address(address):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO sources (address) VALUES (?)", (address,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка добавления адреса: {e}")
        return False
    finally:
        conn.close()


def delete_source_address(address):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM sources WHERE address = ?", (address,))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Ошибка удаления адреса: {e}")
        return False
    finally:
        conn.close()


def get_source_addresses():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT address FROM sources")
    addresses = [row[0] for row in cursor.fetchall()]
    conn.close()
    return addresses


# Проверка обработки транзакции
def is_transaction_processed(signature):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_txs WHERE signature = ?", (signature,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def mark_transaction_processed(signature):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO processed_txs (signature, timestamp) VALUES (?, ?)",
                   (signature, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()


# Проверка уведомления о кошельке
def is_wallet_notified(wallet_address):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM notified_wallets WHERE wallet_address = ?", (wallet_address,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def mark_wallet_notified(wallet_address):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO notified_wallets (wallet_address) VALUES (?)",
                   (wallet_address,))
    conn.commit()
    conn.close()


# Валидация адреса Solana
def is_valid_solana_address(address):
    return re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address) is not None


# Получение исходящих транзакций с адреса
async def get_outgoing_transactions(address, before=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            address,
            {
                "limit": 10,
                "before": before
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SOLANA_RPC_URL, json=payload, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Ошибка получения транзакций: HTTP {response.status}")
                    logger.error(f"Ответ: {await response.text()}")
                    return []
                result = await response.json()
                return result.get('result', [])
    except Exception as e:
        logger.error(f"Ошибка получения транзакций для {address}: {e}")
        return []


# Получение деталей транзакции
async def get_transaction_details(signature):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "json",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SOLANA_RPC_URL, json=payload, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Ошибка получения деталей транзакции: HTTP {response.status}")
                    logger.error(f"Ответ: {await response.text()}")
                    return None
                result = await response.json()
                return result.get('result')
    except Exception as e:
        logger.error(f"Ошибка получения деталей транзакции {signature}: {e}")
        return None


# Отправка уведомления в Telegram
async def send_notification(context: ContextTypes.DEFAULT_TYPE, wallet, amount, source, timestamp):
    # Получаем настройки для отображения времени в правильном часовом поясе
    settings = get_settings()
    tz_offset = int(settings.get('timezone', '0'))
    tz = timezone(timedelta(hours=tz_offset))

    # Форматируем время
    dt = datetime.fromtimestamp(timestamp, tz=tz)
    time_str = dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    # ФОРМАТ УВЕДОМЛЕНИЯ
    message = (
        f"🔥 New wallet detected!\n"
        f"• Wallet: `{wallet}`\n"
        f"• First deposit: {amount:.6f} SOL\n"
        f"• From source: `{source}`\n"
        f"• Time: {time_str}"
    )

    logger.info(f"📤 Отправка уведомления для кошелька {wallet}, сумма: {amount:.6f} SOL")

    try:
        # Проверка, что контекст и бот доступны
        if context is None or context.bot is None:
            logger.error("❌ Контекст или бот не инициализированы")
            return False

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=message,
            parse_mode="Markdown"
        )
        logger.info(f"✅ Уведомление успешно отправлено в Telegram для кошелька {wallet}")
        mark_wallet_notified(wallet)
        return True
    except Exception as e:
        logger.error(f"❌ ОШИБКА отправки уведомления в Telegram: {e}")
        logger.error(
            f"Проверьте: 1) Правильность ADMIN_USER_ID ({ADMIN_USER_ID}), 2) Правильность BOT_TOKEN, 3) Заблокировал ли вас пользователь")
        return False


# Анализ транзакции для поиска переводов SOL от нашего источника
def analyze_transaction(tx_details, source_address, settings):
    """
    Анализирует транзакцию для поиска переводов SOL от указанного источника
    Возвращает кортеж (найден_перевод, адрес_получателя, сумма_в_SOL, информация_для_лога)
    """
    try:
        if not tx_details or 'transaction' not in tx_details or 'meta' not in tx_details:
            logger.debug("❌ Транзакция не содержит необходимых данных")
            return False, None, 0, "Некорректная структура транзакции"

        transaction = tx_details['transaction']
        meta = tx_details['meta']

        if 'message' not in transaction:
            logger.debug("❌ Транзакция не содержит секции message")
            return False, None, 0, "Отсутствует секция message"

        message = transaction['message']
        account_keys = message.get('accountKeys', [])

        if not account_keys:
            logger.debug("❌ Транзакция не содержит accountKeys")
            return False, None, 0, "Отсутствуют accountKeys"

        logger.debug(f"📋 Счета в транзакции: {account_keys}")

        # Находим индекс нашего адреса-источника
        try:
            source_index = account_keys.index(source_address)
        except ValueError:
            logger.debug(f"⏭️ Адрес источника {source_address} не найден в транзакции")
            return False, None, 0, "Источник не найден в транзакции"

        # Проверяем изменения баланса для нашего адреса
        if 'preBalances' not in meta or 'postBalances' not in meta:
            logger.debug("❌ Отсутствуют данные о балансах в meta")
            return False, None, 0, "Отсутствуют данные о балансах"

        pre_balance = meta['preBalances'][source_index]
        post_balance = meta['postBalances'][source_index]
        fee = meta.get('fee', 0)

        # Изменение баланса = предыдущий баланс - текущий баланс - комиссия
        balance_change = pre_balance - post_balance - fee

        # Если баланс увеличился, это не исходящий перевод
        if balance_change <= 0:
            logger.debug(f"⏭️ Баланс адреса-источника не уменьшился (изменение: {balance_change})")
            return False, None, 0, "Нет исходящего перевода с источника"

        # Переводим lamports в SOL
        amount_sol = balance_change / 1_000_000_000

        # Проверяем фильтры суммы
        min_amount = float(settings['min_amount'])
        max_amount = float(settings['max_amount'])

        if not (min_amount <= amount_sol <= max_amount):
            logger.debug(f"⏭️ Сумма {amount_sol:.6f} SOL вне диапазона ({min_amount}-{max_amount})")
            return False, None, 0, f"Сумма вне диапазона: {amount_sol:.6f} SOL"

        # Теперь ищем получателя перевода
        # Для этого анализируем инструкции на предмет перевода
        instructions = message.get('instructions', [])
        recipient = None

        for instruction in instructions:
            # Случай 1: Распарсенная инструкция
            if 'parsed' in instruction and 'info' in instruction['parsed']:
                parsed = instruction['parsed']
                info = parsed['info']
                instruction_type = parsed.get('type', '')

                if instruction_type == 'transfer':
                    if info.get('source') == source_address:
                        recipient = info.get('destination')
                        break

            # Случай 2: Сырая инструкция для системного перевода
            elif 'programIdIndex' in instruction:
                program_id_index = instruction['programIdIndex']
                if program_id_index < len(account_keys) and account_keys[
                    program_id_index] == '11111111111111111111111111111111':
                    # Это системная инструкция
                    accounts = instruction.get('accounts', [])
                    if len(accounts) >= 3:
                        source_acc_index = accounts[0]  # Отправитель обычно первый
                        dest_acc_index = accounts[1]  # Получатель обычно второй

                        if source_acc_index < len(account_keys) and dest_acc_index < len(account_keys):
                            if account_keys[source_acc_index] == source_address:
                                recipient = account_keys[dest_acc_index]
                                break

        # Если не нашли получателя через инструкции, пробуем другой метод
        if not recipient:
            # Находим аккаунт, баланс которого увеличился примерно на сумму перевода
            for i, (pre, post) in enumerate(zip(meta['preBalances'], meta['postBalances'])):
                if i == source_index:
                    continue

                balance_diff = post - pre
                # Учитываем погрешность из-за комиссий
                if abs(balance_diff - balance_change) < 1000000:  # 0.001 SOL в lamports
                    recipient = account_keys[i]
                    break

        if not recipient:
            logger.debug("⏭️ Не удалось определить получателя перевода")
            return False, None, 0, "Получатель не определен"

        # Проверяем, не уведомляли ли уже об этом кошельке
        if is_wallet_notified(recipient):
            logger.debug(f"⏭️ Кошелек {recipient} уже был уведомлен ранее")
            return False, None, 0, "Кошелек уже был уведомлен"

        logger.info(f"✅ Обнаружен перевод: {source_address} -> {recipient}, сумма: {amount_sol:.6f} SOL")
        return True, recipient, amount_sol, f"Перевод обнаружен: {amount_sol:.6f} SOL к {recipient}"

    except Exception as e:
        logger.error(f"❌ Ошибка анализа транзакции: {e}")
        logger.exception("Полная ошибка:")
        return False, None, 0, f"Ошибка анализа: {str(e)}"


# Проверка транзакций для всех адресов-источников
async def check_transactions(context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔍 Начало проверки транзакций...")
    sources = get_source_addresses()
    if not sources:
        logger.warning("📭 Нет адресов-источников для проверки. Добавьте адреса с помощью команды /addsource")
        return

    settings = get_settings()
    min_amount = float(settings['min_amount'])
    max_amount = float(settings['max_amount'])
    notify_all = settings.get('notify_all_transactions', 'true').lower() == 'true'

    logger.info(f"⚙️ Настройки: min={min_amount}, max={max_amount}, notify_all={notify_all}")
    logger.info(f"📦 Источников для проверки: {len(sources)}")

    for source_address in sources:
        logger.info(f"🔍 Проверка транзакций для адреса: {source_address}")
        transactions = await get_outgoing_transactions(source_address)

        if not transactions:
            logger.info(f"📭 Нет новых транзакций для адреса {source_address}")
            continue

        logger.info(f"📄 Найдено транзакций: {len(transactions)}")

        for tx in transactions:
            signature = tx['signature']
            slot_time = tx.get('blockTime', int(datetime.now().timestamp()))

            # Пропускаем уже обработанные транзакции
            if is_transaction_processed(signature):
                logger.debug(f"⏭️ Транзакция {signature} уже обработана")
                continue

            # Получаем детали транзакции
            tx_details = await get_transaction_details(signature)
            if not tx_details:
                logger.warning(f"⚠️ Не удалось получить детали транзакции {signature}")
                mark_transaction_processed(signature)
                continue

            # Анализируем транзакцию
            found_transfer, recipient, amount_sol, log_info = analyze_transaction(
                tx_details, source_address, settings
            )

            if found_transfer:
                # Отправляем уведомление
                success = await send_notification(
                    context,
                    recipient,
                    amount_sol,
                    source_address,
                    slot_time
                )

                if success:
                    logger.info(f"✅ Уведомление успешно отправлено для кошелька {recipient}")
                else:
                    logger.error(f"❌ Не удалось отправить уведомление для кошелька {recipient}")
            else:
                logger.info(f"⏭️ {log_info}")

            # Помечаем транзакцию как обработанную в любом случае
            mark_transaction_processed(signature)
            logger.info(f"✅ Транзакция {signature} обработана и помечена как processed")

    logger.info("✅ Проверка транзакций завершена")


async def get_wallet_balance(address):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SOLANA_RPC_URL, json=payload, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Ошибка получения баланса: HTTP {response.status}")
                    return None
                result = await response.json()
                return result.get('result', {}).get('value', 0)
    except Exception as e:
        logger.error(f"Ошибка получения баланса для {address}: {e}")
        return None


# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ У вас нет доступа к этому боту")
        return

    help_text = (
        "👋 Добро пожаловать в Solana Wallet Tracker!\n\n"
        "Доступные команды:\n"
        "/addsource - Добавить адрес-источник\n"
        "/deletesource - Удалить адрес-источник\n"
        "/listsources - Показать список адресов\n"
        "/setrange - Установить диапазон сумм (SOL)\n"
        "/settimezone - Установить часовой пояс (UTC+offset)\n"
        "/setnotifications - Настроить режим уведомлений\n"
        "/clearcache - Очистить кэш обработанных транзакций\n"
        "/settings - Показать текущие настройки"
    )
    await update.message.reply_text(help_text)


async def add_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    await update.message.reply_text(
        "Введите адрес Solana для добавления в список источников:"
    )
    return ADD_SOURCE


async def add_source_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    if not is_valid_solana_address(address):
        await update.message.reply_text(
            "❌ Неверный формат адреса Solana. Попробуйте еще раз:"
        )
        return ADD_SOURCE

    if add_source_address(address):
        await update.message.reply_text(
            f"✅ Адрес успешно добавлен:\n`{address}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка при добавлении адреса. Возможно, он уже существует."
        )

    return ConversationHandler.END


async def delete_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    sources = get_source_addresses()
    if not sources:
        await update.message.reply_text("📭 Список источников пуст")
        return ConversationHandler.END

    message = "Выберите адрес для удаления:\n\n"
    for i, addr in enumerate(sources, 1):
        message += f"{i}. `{addr}`\n"

    await update.message.reply_text(
        message + "\nВведите номер адреса или сам адрес:",
        parse_mode="Markdown"
    )
    return DELETE_SOURCE


async def delete_source_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip()
    sources = get_source_addresses()

    # Проверяем, введен ли номер
    if input_text.isdigit():
        index = int(input_text) - 1
        if 0 <= index < len(sources):
            address = sources[index]
        else:
            await update.message.reply_text("❌ Неверный номер. Попробуйте еще раз:")
            return DELETE_SOURCE
    else:
        address = input_text

    if not is_valid_solana_address(address):
        await update.message.reply_text(
            "❌ Неверный формат адреса. Попробуйте еще раз:"
        )
        return DELETE_SOURCE

    if delete_source_address(address):
        await update.message.reply_text(
            f"✅ Адрес успешно удален:\n`{address}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Адрес не найден в списке источников."
        )

    return ConversationHandler.END


async def list_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    sources = get_source_addresses()
    if not sources:
        await update.message.reply_text("📭 Список источников пуст")
        return

    message = "📋 Список адресов-источников:\n\n"
    for i, addr in enumerate(sources, 1):
        message += f"{i}. `{addr}`\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def set_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    await update.message.reply_text(
        "Введите минимальную сумму в SOL (например, 0.001):"
    )
    return SET_RANGE_MIN


async def set_range_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_amount = float(update.message.text.strip())
        if min_amount < 0:
            raise ValueError
        context.user_data['min_amount'] = min_amount
        await update.message.reply_text(
            "Введите максимальную сумму в SOL (например, 10):"
        )
        return SET_RANGE_MAX
    except ValueError:
        await update.message.reply_text(
            "❌ Неверное значение. Введите положительное число:"
        )
        return SET_RANGE_MIN


async def set_range_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        max_amount = float(update.message.text.strip())
        min_amount = context.user_data.get('min_amount', 0)

        if max_amount < min_amount:
            await update.message.reply_text(
                f"❌ Максимальное значение должно быть больше минимального ({min_amount}). Попробуйте еще раз:"
            )
            return SET_RANGE_MAX

        update_setting('min_amount', str(min_amount))
        update_setting('max_amount', str(max_amount))

        await update.message.reply_text(
            f"✅ Диапазон успешно установлен:\n"
            f"Минимум: {min_amount} SOL\n"
            f"Максимум: {max_amount} SOL"
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            "❌ Неверное значение. Введите положительное число:"
        )
        return SET_RANGE_MAX


async def set_timezone_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    await update.message.reply_text(
        "Введите смещение часового пояса от UTC (например, 5 для UTC+5):"
    )
    return SET_TIMEZONE


async def set_timezone_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tz_offset = int(update.message.text.strip())
        if not -12 <= tz_offset <= 14:
            raise ValueError

        update_setting('timezone', str(tz_offset))
        await update.message.reply_text(
            f"✅ Часовой пояс установлен: UTC{tz_offset:+d}"
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            "❌ Неверное значение. Введите целое число от -12 до 14:"
        )
        return SET_TIMEZONE


async def set_notification_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    settings = get_settings()
    current_mode = settings.get('notify_all_transactions', 'true').lower() == 'true'

    new_mode = not current_mode
    update_setting('notify_all_transactions', str(new_mode).lower())

    mode_text = "ВСЕ транзакции" if new_mode else "только первые транзакции"
    await update.message.reply_text(
        f"✅ Режим уведомлений изменен:\n"
        f"Теперь будут отслеживаться {mode_text}"
    )
    return ConversationHandler.END


async def clear_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    clear_test_data()
    await update.message.reply_text("✅ Кэш обработанных транзакций и уведомленных кошельков очищен")


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    settings = get_settings()
    sources = get_source_addresses()

    tz_offset = int(settings['timezone'])
    notify_all = settings.get('notify_all_transactions', 'true').lower() == 'true'
    notify_mode = "Все транзакции" if notify_all else "Только первые транзакции"

    message = (
        "⚙️ Текущие настройки:\n\n"
        f"🕒 Часовой пояс: UTC{tz_offset:+d}\n"
        f"💰 Диапазон сумм: {float(settings['min_amount']):.6f} - {float(settings['max_amount']):.4f} SOL\n"
        f"🔔 Режим уведомлений: {notify_mode}\n"
        f"📦 Адресов-источников: {len(sources)}\n\n"
        f"👤 ADMIN_USER_ID: {ADMIN_USER_ID}"
    )

    if sources:
        message += "\n\n📋 Список адресов-источников:"
        for i, addr in enumerate(sources, 1):
            message += f"\n{i}. `{addr}`"

    await update.message.reply_text(message, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END


async def post_init(application: Application) -> None:
    # Проверяем, доступен ли JobQueue
    if application.job_queue is None:
        logger.error("JobQueue не инициализирован! Установите зависимости с [job-queue]")
        return

    # Проверка подключения к Telegram API
    try:
        bot_info = await application.bot.get_me()
        logger.info(f"✅ Успешное подключение к Telegram API. Бот: @{bot_info.username}")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Telegram API: {e}")
        logger.error("Проверьте правильность токена бота!")

    # Запуск фоновой задачи проверки транзакций
    application.job_queue.run_repeating(
        check_transactions,
        interval=15,  # Проверять каждые 15 секунд
        first=1
    )
    logger.info("✅ JobQueue успешно запущен")
    logger.info(f"🚀 Бот запущен и работает с RPC: {SOLANA_RPC_URL}")
    logger.info(f"👤 ADMIN_USER_ID: {ADMIN_USER_ID}")


def main():
    # Инициализация базы данных
    init_db()

    # Очистка кэша при запуске для тестирования
    clear_test_data()

    # Создание приложения с ВАШИМ реальным токеном
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ConversationHandler для добавления адреса
    conv_add_source = ConversationHandler(
        entry_points=[CommandHandler('addsource', add_source_start)],
        states={
            ADD_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_source_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ConversationHandler для удаления адреса
    conv_delete_source = ConversationHandler(
        entry_points=[CommandHandler('deletesource', delete_source_start)],
        states={
            DELETE_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_source_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ConversationHandler для установки диапазона
    conv_set_range = ConversationHandler(
        entry_points=[CommandHandler('setrange', set_range_start)],
        states={
            SET_RANGE_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_range_min)],
            SET_RANGE_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_range_max)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ConversationHandler для установки часового пояса
    conv_set_timezone = ConversationHandler(
        entry_points=[CommandHandler('settimezone', set_timezone_start)],
        states={
            SET_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_timezone_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ConversationHandler для установки режима уведомлений (не требует дополнительных состояний)
    conv_set_notification = ConversationHandler(
        entry_points=[CommandHandler('setnotifications', set_notification_mode)],
        states={
            SET_NOTIFICATION_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_notification_mode)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("listsources", list_sources))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("clearcache", clear_cache))
    application.add_handler(conv_add_source)
    application.add_handler(conv_delete_source)
    application.add_handler(conv_set_range)
    application.add_handler(conv_set_timezone)
    application.add_handler(conv_set_notification)

    # Запуск бота
    logger.info("🚀 Бот запускается...")
    logger.info("📱 Отправьте команду /start в Telegram, чтобы начать работу")
    application.run_polling()


if __name__ == "__main__":
    main()