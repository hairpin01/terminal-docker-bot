import asyncio
import docker
import tempfile
import tarfile
import signal
import sys
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler,
    InlineQueryHandler )

import redis
import logging
import os
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import psutil

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)

logger = logging.getLogger(__name__)

SELECTING_IMAGE, SELECTING_SHELL, SELECTING_TTL, SELECTING_CONFIG, CUSTOM_IMAGE, CONFIRMING_USER, UPLOAD_FILE, DOWNLOAD_FILE = range(8)

class TerminalBot:
    def __init__(self):
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client initialized successfully")
            self.cleanup_old_containers()
            self.setup_signal_handlers()
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            sys.exit(1)

        self.redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

        # Переносим вызов cleanup_old_sessions после инициализации redis
        self.cleanup_old_sessions()

        # Администраторы
        self.admin_ids = []  # Замените на ваши ID

        # Подтвержденные пользователи
        self.init_confirmed_users()

        self.file_limits = {
                'confirmed': {
                'upload': 60 * 1024 * 1024,  # 60 МБ
                'download': 20 * 1024 * 1024  # 20 МБ
                },
                'unconfirmed': {
                'upload': 40 * 1024 * 1024,  # 40 МБ
                'download': 15 * 1024 * 1024  # 15 МБ
                }
        }

        self.available_images = {
            "alpine:latest": "Alpine Linux",
            "ubuntu:latest": "Ubuntu",
            "debian:latest": "Debian",
            "kalilinux/kali-rolling": "Kali Linux",
            "opensuse/leap:latest": "openSUSE",
            "fedora:latest": "Fedora",
            "archlinux:latest": "Arch Linux"
        }

        # Шеллы
        self.available_shells = ["bash", "sh"]

        # Время жизни контейнеров
        self.ttl_options = {
            "30m": 1800,
            "1h": 3600,
            "5h": 18000,
            "24h": 86400,
            "7d": 604800,
            "12d": 1036800,
            "always": None
        }

        # Система токенов
        self.initial_tokens = 480  # Начальное количество токенов
        self.token_consumption_rate = 1  # Токенов в минуту

        # Тестовая конфигурация
        self.test_config = {
            "image": "alpine:latest",
            "shell": "sh",
            "mem_limit": "50m",
            "cpu_quota": 25000,  # 25% CPU
            "cpu_period": 100000,
            "pids_limit": 10,
            "timeout": 80,  # 80 секунд на команду
            "max_session_time": 1200,  # 20 минут
            "no_background": True  # Запрет фоновых процессов
        }

        # Конфигурации ресурсов
        self.resource_configs = {
            "minimal": {
                "name": "Базовая",
                "cpu_period": 100000,
                "cpu_quota": 30000,  # 25% CPU
                "mem_limit": "246m",
                "pids_limit": 25,
                "description": "246MB RAM, 30% CPU"
            },
            "medium": {
                "name": "Средняя",
                "cpu_period": 100000,
                "cpu_quota": 50000,  # 50% CPU
                "mem_limit": "246m",
                "pids_limit": 50,
                "description": "270MB RAM, 50% CPU"
            },
            "enhanced": {
                "name": "Улучшенная",
                "cpu_period": 100000,
                "cpu_quota": 75000,  # 75% CPU
                "mem_limit": "428m",
                "pids_limit": 100,
                "description": "428MB RAM, 75% CPU"
            },
            "maximum": {
                "name": "Максимальная",
                "cpu_period": 100000,
                "cpu_quota": 100000,  # 100% CPU
                "mem_limit": "612m",
                "pids_limit": 200,
                "description": "512MB RAM, 100% CPU"
            }
        }

        # Очереди команд для каждого пользователя
        self.command_queues = {}
        self.command_workers = {}
        self.active_commands = {}

        # Пул потоков для выполнения команд
        self.thread_pool = ThreadPoolExecutor(max_workers=10)


    def cleanup_old_sessions(self):
        """Очищает устаревшие сессии из Redis"""
        try:
            # Получаем все ключи сессий
            session_keys = self.redis.keys("session:*")
            logger.info(f"Found {len(session_keys)} sessions to check")

            for key in session_keys:
                try:
                    session_data = self.redis.get(key)
                    if session_data:
                        session = json.loads(session_data)
                        container_id = session.get('container_id')
                        if container_id:
                            # Проверяем существование контейнера
                            container = self.docker_client.containers.get(container_id)
                            if container.status != 'running':
                                # Удаляем сессию неработающего контейнера
                                self.redis.delete(key)
                                logger.info(f"Removed session for non-running container: {container_id}")
                except docker.errors.NotFound:
                    # Контейнер не найден - удаляем сессию
                    self.redis.delete(key)
                    logger.info(f"Removed session for non-existent container")
                except Exception as e:
                    logger.error(f"Error checking session {key}: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up old sessions: {e}")


    def cleanup_old_containers(self):
        """Очищает старые контейнеры бота при запуске"""
        try:
            containers = self.docker_client.containers.list(
                all=True,
                filters={"name": "terminal_bot_"}
            )
            logger.info(f"Found {len(containers)} old containers to clean up")
            for container in containers:
                try:
                    logger.info(f"Останавливаем контейнер: {container.name} (ID: {container.id})")
                    container.stop(timeout=1)
                    container.remove()
                    logger.info(f"Удален старый контейнер: {container.name}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении контейнера {container.name}: {e}")
        except Exception as e:
            logger.error(f"Ошибка при очистке контейнеров: {e}")


    def init_confirmed_users(self):
        """Инициализирует список подтвержденных пользователей"""
        if not self.redis.exists("confirmed_users"):
            for admin_id in self.admin_ids:
                self.redis.sadd("confirmed_users", admin_id)
            logger.info(f"Initialized confirmed users with admin IDs: {self.admin_ids}")

    def is_admin(self, user_id):
        """Проверяет, является ли пользователь администратором"""
        return str(user_id) in [str(admin_id) for admin_id in self.admin_ids]

    def is_confirmed_user(self, user_id):
        """Проверяет, является ли пользователь подтвержденным"""
        return self.redis.sismember("confirmed_users", str(user_id))

    def add_confirmed_user(self, user_id):
        """Добавляет пользователя в подтвержденные"""
        self.redis.sadd("confirmed_users", str(user_id))
        logger.info(f"Added user {user_id} to confirmed users")

    def has_active_session(self, user_id):
        """Проверяет, есть ли у пользователя активная сессия"""
        session_key = f"session:{user_id}"
        return self.redis.exists(session_key)

    def get_session_info(self, user_id):
        """Получает информацию о сессии пользователя"""
        session_key = f"session:{user_id}"
        session_data = self.redis.get(session_key)
        if session_data:
            return json.loads(session_data)
        return None

    def setup_signal_handlers(self):
        """Настраивает обработчики сигналов для graceful shutdown"""
        def signal_handler(signum, frame):
            logger.info(f"Получен сигнал {signum}, завершаем работу...")
            self.cleanup_all_containers()
            # Останавливаем все воркеры
            for user_id, worker in self.command_workers.items():
                worker.cancel()
            self.thread_pool.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def init_user_tokens(self, user_id):
        """Инициализирует токены для нового пользователя"""
        token_key = f"tokens:{user_id}"
        if not self.redis.exists(token_key):
            self.redis.set(token_key, self.initial_tokens)
            logger.info(f"Initialized {self.initial_tokens} tokens for user {user_id}")

    def get_user_tokens(self, user_id):
        """Получает количество токенов пользователя"""
        token_key = f"tokens:{user_id}"
        tokens = self.redis.get(token_key)
        return int(tokens) if tokens else 0

    def consume_tokens(self, user_id, minutes=1):
        """Списывает токены за использование"""
        if self.is_confirmed_user(user_id):
            return True  # Подтвержденные пользователи не тратят токены

        token_key = f"tokens:{user_id}"
        current_tokens = self.get_user_tokens(user_id)

        if current_tokens <= 0:
            return False

        new_tokens = max(0, current_tokens - minutes)
        self.redis.set(token_key, new_tokens)
        logger.info(f"Consumed {minutes} tokens for user {user_id}, remaining: {new_tokens}")
        return True

    def add_tokens(self, user_id, amount):
        """Добавляет токены пользователю"""
        token_key = f"tokens:{user_id}"
        current_tokens = self.get_user_tokens(user_id)
        new_tokens = current_tokens + amount
        self.redis.set(token_key, new_tokens)
        logger.info(f"Added {amount} tokens to user {user_id}, total: {new_tokens}")

        async def _token_consumption_worker(self, user_id):
            """Фоновая задача для потребления токенов"""
            consumption_key = f"token_consumption:{user_id}"

            while True:
                try:
                    await asyncio.sleep(60)  # Проверяем каждую минуту

                    # Проверяем, активен ли еще контейнер
                    if not self.has_active_session(user_id):
                        break

                    consumption_data = self.redis.get(consumption_key)
                    if not consumption_data:
                        break

                    # Списываем токены
                    if not self.consume_tokens(user_id, 1):
                        # Токены закончились, останавливаем контейнер
                        await self.stop_session_due_to_tokens(user_id)
                        break

                    # Обновляем время последнего списания
                    consumption = json.loads(consumption_data)
                    consumption['last_consumption'] = datetime.now().isoformat()
                    self.redis.set(consumption_key, json.dumps(consumption))

                except Exception as e:
                    logger.error(f"Error in token consumption worker for user {user_id}: {e}")
                    break

        async def stop_session_due_to_tokens(self, user_id):
            """Останавливает сессию из-за нехватки токенов"""
            session_info = self.get_session_info(user_id)

            # Останавливаем контейнер
            container_id = session_info.get('container_id')
            if container_id:
                try:
                    container = self.docker_client.containers.get(container_id)
                    container.stop()
                    container.remove()
                    logger.info(f"Stopped container {container_id} for user {user_id} due to token exhaustion")
                except Exception as e:
                    logger.error(f"Error stopping container: {e}")

            # Удаляем сессию
            session_key = f"session:{user_id}"
            self.redis.delete(session_key)

            # Удаляем информацию о потреблении токенов
            consumption_key = f"token_consumption:{user_id}"
            self.redis.delete(consumption_key)

            # Отправляем уведомление пользователю
            try:
                from telegram import Update
                # Создаем fake update для отправки сообщения
                class FakeUpdate:
                    def __init__(self, user_id):
                        self.effective_user = type('User', (), {'id': user_id})()

                fake_update = FakeUpdate(user_id)
                await self.show_token_exhausted_menu(fake_update, None)
            except Exception as e:
                logger.error(f"Error sending token exhaustion message: {e}")


    async def nohup_command(self, update: Update, context: CallbackContext):
        """Асинхронно выполняет команду в фоне с проверкой для тестовых контейнеров"""
        user_id = update.effective_user.id

        # Проверяем, есть ли активная сессия
        if not self.has_active_session(user_id):
            await update.message.reply_text(
                "❌ У вас нет активного контейнера. Используйте /container для создания нового."
            )
            return

        # Проверяем, не тестовый ли это контейнер
        session_info = self.get_session_info(user_id)
        if session_info.get('is_test', False):
            await update.message.reply_text(
                "❌ В тестовом контейнере нельзя запускать команды в фоне.\n\n"
                "💡 Используйте обычный контейнер для фоновых процессов."
            )
            return

        # Проверяем аргументы команды
        if not context.args:
            await update.message.reply_text(
                "Использование: /nohup <команда>\n\n"
                "Примеры:\n"
                "/nohup python -m http.server 8080\n"
                "/nohup node server.js\n"
                "/nohup ./start.sh"
            )
            return

        command = ' '.join(context.args)

        # Проверка безопасности для неподтвержденных пользователей
        if not self.is_confirmed_user(user_id) and await self.is_command_dangerous(command):
            await update.message.reply_text("❌ Команда запрещена для выполнения")
            return

        # Получаем информацию о сессии
        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')
        shell = session_info.get('shell', 'bash')

        if not container_id:
            await update.message.reply_text("❌ Ошибка: ID контейнера не найден")
            return

        try:
            container = self.docker_client.containers.get(container_id)
        except:
            await update.message.reply_text("❌ Контейнер не найден. Используйте /container для создания нового.")
            self.redis.delete(f"session:{user_id}")
            return

        # Создаем уникальное имя для лог-файла
        import time
        log_file = f"/tmp/nohup_{user_id}_{int(time.time())}.log"

        # Формируем команду для выполнения в фоне
        background_command = f"nohup {shell} -c \"{command}\" > {log_file} 2>&1 & echo $! > /tmp/last_pid_{user_id}.txt"

        # Отправляем сообщение о запуске
        status_msg = await update.message.reply_text("⏳ Запускаю команду в фоне...")

        # Запускаем асинхронное выполнение
        asyncio.create_task(
            self._execute_nohup_command(
                user_id, container, background_command, command, log_file, status_msg
            )
        )

    async def _execute_nohup_command(self, user_id, container, background_command, original_command, log_file, status_msg):
        """Асинхронно выполняет nohup команду"""
        try:
            # Выполняем команду запуска в фоне
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self.thread_pool,
                self._run_command_sync,
                container,
                background_command
            )

            output, exit_code = result

            if exit_code == 0:
                # Получаем PID запущенного процесса
                pid_result = await loop.run_in_executor(
                    self.thread_pool,
                    self._run_command_sync,
                    container,
                    f"cat /tmp/last_pid_{user_id}.txt"
                )

                pid = pid_result[0].strip() if pid_result[0] else "неизвестен"

                await status_msg.edit_text(
                    f"✅ Команда запущена в фоне!\n\n"
                    f"📝 Команда: `{original_command}`\n"
                    f"🆔 PID: `{pid}`\n"
                    f"📁 Логи: `{log_file}`\n\n"
                    f"💡 Для проверки процессов используйте: `ps aux | grep {pid}`\n"
                    f"📋 Для просмотра логов: `tail -f {log_file}`",
                    parse_mode='Markdown'
                )
            else:
                await status_msg.edit_text(
                    f"❌ Ошибка при запуске команды в фоне:\n```\n{output}\n```",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error executing nohup command for user {user_id}: {e}")
            try:
                await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
            except:
                pass




    async def show_token_exhausted_menu(self, update: Update, context: CallbackContext):
        """Показывает меню когда токены закончились"""
        user_id = update.effective_user.id

        keyboard = [
            [InlineKeyboardButton("🧪 Создать тестовый контейнер", callback_data=f"image:{user_id}:test")],
            [InlineKeyboardButton("🔄 Проверить токены", callback_data=f"token_info:{user_id}")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            "🔴 Токены закончились!\n\n"
            "🎫 У вас закончились токены для использования обычных контейнеров.\n\n"
            "💡 Доступные опции:\n"
            "• 🧪 Создать тестовый контейнер (бесплатно, с ограничениями)\n"
            "• 🔄 Проверить баланс токенов\n"
            "• 📞 Обратиться к администратору для пополнения\n\n"
            "🧪 Тестовый контейнер включает:\n"
            "• Alpine Linux образ\n"
            "• 50MB RAM, 25% CPU\n"
            "• Таймаут команд: 80 секунд\n"
            "• Время жизни: 20 минут\n"
            "• Без фоновых процессов"
        )

        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)

    async def show_token_info(self, update: Update, context: CallbackContext):
        """Показывает информацию о токенах"""
        query = update.callback_query
        user_id = query.from_user.id

        if self.is_confirmed_user(user_id):
            tokens_text = "∞ (безлимит)"
        else:
            tokens = self.get_user_tokens(user_id)
            tokens_text = f"{tokens} 🎫"

            # Показываем когда пополнятся токены (например, +10 в день)
            next_refill = "завтра"  # Можно реализовать логику пополнения

        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data=f"main:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🎫 Информация о токенах\n\n"
            f"👤 Ваш статус: {'✅ Подтвержденный' if self.is_confirmed_user(user_id) else '⏳ Обычный'}\n"
            f"📊 Доступно токенов: {tokens_text}\n\n"
            f"💡 Токены тратятся:\n"
            f"• 1 токен в минуту за обычный контейнер\n"
            f"• Тестовые контейнеры бесплатны\n\n"
            f"🔧 Обычные контейнеры:\n"
            f"• Все образы и шеллы\n"
            f"• Нет таймаута команд\n"
            f"• Можно запускать в фоне\n\n"
            f"🧪 Тестовые контейнеры:\n"
            f"• Только Alpine + sh\n"
            f"• Таймаут 80 секунд\n"
            f"• 20 минут времени жизни\n"
            f"• Без фоновых процессов",
            reply_markup=reply_markup
        )


    async def background_processes(self, update: Update, context: CallbackContext):
        """Показывает запущенные фоновые процессы"""
        user_id = update.effective_user.id

        if not self.has_active_session(user_id):
            await update.message.reply_text("❌ У вас нет активного  контейнера")
            return

        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')

        try:
            container = self.docker_client.containers.get(container_id)

            # Получаем список процессов
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self.thread_pool,
                self._run_command_sync,
                container,
                "ps aux --sort=-%cpu | head -20"
            )

            output, exit_code = result

            if exit_code == 0:
                await update.message.reply_text(
                    f"📊 Запущенные процессы (топ-20 по CPU):\n```\n{output}\n```",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Ошибка при получении списка процессов")

        except Exception as e:
            logger.error(f"Error getting processes for user {user_id}: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    async def kill_process(self, update: Update, context: CallbackContext):
        """Останавливает процесс по PID"""
        user_id = update.effective_user.id

        if not context.args:
            await update.message.reply_text("Использование: /kill       <PID>")
            return

        pid = context.args[0]

        if not pid.isdigit():
            await update.message.reply_text("❌ PID должен быть числом")
            return

        if not self.has_active_session(user_id):
            await update.message.reply_text("❌ У вас нет активного контейнера")
            return

        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')

        try:
            container = self.docker_client.containers.get(container_id)

            # Останавливаем процесс
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self.thread_pool,
                self._run_command_sync,
                container,
                f"kill {pid}"
            )

            output, exit_code = result

            if exit_code == 0:
                await update.message.reply_text(f"✅ Процесс {pid} остановлен")
            else:
                await update.message.reply_text(
                    f"❌ Ошибка при остановке процесса {pid}:    \n```\n{output}\n```",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Error killing process {pid} for user {user_id}: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")


    async def check_callback_access(self, update: Update, context: CallbackContext, user_id: int = None) -> bool:
        """Проверяет, имеет ли пользователь доступ к этому callback"""
        query = update.callback_query
        if user_id is None:
            user_id = query.from_user.id

        # Получаем user_id из callback_data если есть
        callback_data = query.data
        if ':' in callback_data:
            try:
                parts = callback_data.split(':')
                if len(parts) >= 2:
                    target_user_id = int(parts[-1])
                    if user_id != target_user_id:
                        await query.answer("❌ Это меню не для вас!", show_alert=True)
                        return False
            except:
                pass

        return True

    async def start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name

        logger.info(f"User {user_id} ({username}) started the bot")

        await update.message.reply_text(
            "Привет! 👋\n\n"
            "Я - Docker Terminal Bot, твой личный терминал в Telegram!\n\n"
            "✨ Что я умею:\n"
            "• Запускать изолированные Docker-контейнеры\n"
            "• Выполнять команды в реальном времени\n"
            "• Работать с разными ОС и шеллами\n"
            "• Ограничивать ресурсы для безопасности\n\n"
            "🚀 Чтобы запустить свой первый контейнер, пропиши: /container\n\n"
            "💡 Подсказка: Используй /docker <команда> в группах для выполнения команд!"
        )

    async def container_command(self, update: Update, context: CallbackContext):
        """Обработчик команды /container - показывает главное меню"""
        user_id = update.effective_user.id
        await self.show_main_menu(update, context, user_id)

    async def start_command_worker(self, user_id):
        """Запускает воркер для обработки команд пользователя"""
        if user_id in self.command_workers:
            return

        async def worker():
            queue = self.command_queues.get(user_id)
            if not queue:
                return

            while True:
                try:
                    # Получаем команду из очереди с таймаутом
                    try:
                        update, context, command, status_msg = await asyncio.wait_for(
                            queue.get(), timeout=300.0  # 5 минут таймаут
                        )
                    except asyncio.TimeoutError:
                        # Если очередь пуста 5 минут, завершаем воркер
                        break

                    # Выполняем команду
                    await self._execute_single_command(update, context, command, status_msg, user_id)

                    # Помечаем задачу как выполненную
                    queue.task_done()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in command worker for user {user_id}: {e}")
                    try:
                        await status_msg.edit_text(f"❌ Ошибка при выполнении команды: {str(e)}")
                    except:
                        pass

        # Запускаем воркер
        worker_task = asyncio.create_task(worker())
        self.command_workers[user_id] = worker_task

    async def _command_worker(self, user_id):
        """Воркер для обработки команд пользователя"""
        while True:
            try:
                # Ждем команду из очереди
                command_data = await self.command_queues[user_id].get()

                if command_data is None:  # Сигнал остановки
                    break

                update, context, command, status_msg = command_data

                # Выполняем команду
                await self._execute_single_command(update, context, command, status_msg, user_id)

                # Помечаем задачу как выполненную
                self.command_queues[user_id].task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in command worker for user {user_id}: {e}")
                try:
                    await status_msg.edit_text(f"❌ Ошибка воркера: {str(e)}")
                except:
                    pass


    async def show_main_menu(self, update: Update, context: CallbackContext, user_id: int):
        """Показывает главное меню с информацией о токенах"""
        has_session = self.has_active_session(user_id)

        # Инициализируем токены для новых пользователей
        if not self.is_confirmed_user(user_id):
            self.init_user_tokens(user_id)

        if has_session:
            session_info = self.get_session_info(user_id)
            image_name = self.available_images.get(session_info.get('image', ''), session_info.get('image', 'Кастомный'))
            shell = session_info.get('shell', 'bash')
            ttl = session_info.get('ttl_display', 'Неизвестно')
            config_name = session_info.get('config_name', 'Минимальная')
            network = "включена" if session_info.get('network', True) else "выключена"
            is_test = session_info.get('is_test', False)

            # Информация о токенах
            if self.is_confirmed_user(user_id):
                token_info = "✅ Подтвержденный (безлимит)"
            else:
                tokens = self.get_user_tokens(user_id)
                if is_test:
                    token_info = "🧪 Тестовый режим"
                else:
                    token_info = f"🎫 Токены: {tokens}"

            keyboard = [
                [InlineKeyboardButton("🔄 Пересоздать конвейер", callback_data=f"launch:{user_id}")],
                [InlineKeyboardButton("⏹️ Остановить конвейер", callback_data=f"stop:{user_id}")],
                [InlineKeyboardButton("📊 Состояние конвейера", callback_data=f"status:{user_id}")],
                [InlineKeyboardButton("🎫 Информация о токенах", callback_data=f"token_info:{user_id}")],
                [InlineKeyboardButton("ℹ️ Информация", callback_data=f"info:{user_id}")]
            ]

            text = (f"🔧 Главное меню терминал бота\n\n"
                f"✅ Активный конвейер:\n"
                f"🐧 Образ: {image_name}\n"
                f"💻 Шелл: {shell}\n"
                f"⚙️ Конфигурация: {config_name}\n"
                f"🌐 Сеть: {network}\n"
                f"⏰ Время жизни: {ttl}\n"
                f"💳 Статус: {token_info}\n\n"
                f"Выберите действие:")
        else:
            # Информация о токенах для меню без активной сессии
            if self.is_confirmed_user(user_id):
                token_info = "✅ Подтвержденный пользователь"
            else:
                tokens = self.get_user_tokens(user_id)
                token_info = f"🎫 Доступно токенов: {tokens}"

            keyboard = [
                [InlineKeyboardButton("🚀 Запустить конвейер", callback_data=f"launch:{user_id}")],
                [InlineKeyboardButton("🎫 Информация о токенах", callback_data=f"token_info:{user_id}")],
                [InlineKeyboardButton("ℹ️ Информация", callback_data=f"info:{user_id}")]
            ]

            text = f"🔧 Главное меню терминал бота\n\n{token_info}\n\nВыберите действие:"

        if self.is_admin(user_id):
            keyboard.append([InlineKeyboardButton("👑 Админ панель", callback_data=f"admin:{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)

    async def handle_callback(self, update: Update, context: CallbackContext):
        """Универсальный обработчик callback"""
        query = update.callback_query
        await query.answer()

        callback_data = query.data
        user_id = query.from_user.id

        if not await self.check_callback_access(update, context, user_id):
            return

        # Разбираем callback_data
        parts = callback_data.split(':')
        action = parts[0]
        target_user_id = int(parts[1]) if len(parts) > 1 else user_id

        if action == "main":
            await self.show_main_menu(update, context, user_id)
        elif action == "token_info":
            await self.show_token_info(update, context)
        elif action == "launch":
            await self.launch_menu(update, context)
        elif action == "stop":
            await self.stop_session(update, context)
        elif action == "status":
            await self.container_status(update, context)
        elif action == "info":
            await self.information_menu(update, context)
        elif action == "admin":
            await self.admin_menu(update, context)
        elif action == "image":
            image_key = parts[2]
            await self.select_image(update, context, image_key)
        elif action == "shell":
            shell = parts[2]
            await self.select_shell(update, context, shell)
        elif action == "config":
            config_key = parts[2]
            await self.select_config(update, context, config_key)
        elif action == "network":
            network_state = parts[2] == "true"
            await self.toggle_network(update, context, network_state)
        elif action == "ttl":
            ttl_name = parts[2]
            await self.select_ttl(update, context, ttl_name)
        elif action == "custom":
            await self.custom_image_input(update, context)
        elif action == "user_manage":
            await self.user_management(update, context)
        elif action == "container_manage":
            await self.container_management(update, context)
        elif action == "admin_stats":
            await self.admin_stats(update, context)
        elif action == "add_user":
            await self.add_user_prompt(update, context)
        elif action == "confirm_user":
            await self.confirm_add_user(update, context)

    async def container_status(self, update: Update, context: CallbackContext):
      """Показывает состояние контейнера пользователя"""
      query = update.callback_query
      user_id = query.from_user.id

      session_info = self.get_session_info(user_id)
      if not session_info:
          await query.edit_message_text("❌ У вас нет активного контейнера.")
          return

      container_id = session_info.get('container_id')
      if not container_id:
          await query.edit_message_text("❌ Ошибка: ID контейнера не найден")
          return

      try:
          container =   self.docker_client.containers.get(container_id)
          stats = container.stats(stream=False)

          # Анализируем статистику
          cpu_stats = stats['cpu_stats']
          precpu_stats = stats['precpu_stats']
          memory_stats = stats['memory_stats']

          # Расчет использования CPU
          cpu_delta = cpu_stats['cpu_usage']['total_usage'] - precpu_stats['cpu_usage']['total_usage']
          system_delta = cpu_stats['system_cpu_usage'] - precpu_stats['system_cpu_usage']
          cpu_percent = 0.0
          if system_delta > 0 and cpu_delta > 0:
              cpu_percent = (cpu_delta / system_delta) * 100.0

          # Использование памяти
          memory_usage = memory_stats.get('usage', 0)
          memory_limit = memory_stats.get('limit', 1)
          memory_percent = (memory_usage / memory_limit) * 100.0

        # Информация о контейнере
          image_name = self.available_images.get(session_info.get('image', ''), session_info.get('image', 'Кастомный'))
          shell = session_info.get('shell', 'bash')
          config_name = session_info.get('config_name', 'Минимальная')
          network = "включена" if session_info.get('network', True) else "выключена"
          created_at = session_info.get('created_at', 'Неизвестно')

          # Очередь команд
          queue_size = self.command_queues.get(user_id, asyncio.Queue()).qsize() if user_id in self.command_queues else 0

          keyboard = [
              [InlineKeyboardButton("🔄 Обновить", callback_data=f"status:{user_id}")],
              [InlineKeyboardButton("⏹️ Остановить конвейер", callback_data=f"stop:{user_id}")],
              [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
          ]
          reply_markup = InlineKeyboardMarkup(keyboard)

          await query.edit_message_text(
              f"📊 Состояние конвейера\n\n"
              f"🐧 Образ: {image_name}\n"
              f"💻 Шелл: {shell}\n"
              f"⚙️ Конфигурация: {config_name}\n"
              f"🌐 Сеть: {network}\n"
              f"📅 Создан: {created_at[:19]}\n\n"
              f"📈 Использование ресурсов:\n"
              f"• CPU: {cpu_percent:.1f}%\n"
              f"• Память: {memory_percent:.1f}% ({memory_usage // (1024*1024)}MB / {memory_limit // (1024*1024)}MB)\n"
              f"• Команд в очереди: {queue_size}\n\n"
              f"🟢 Контейнер работает нормально",
              reply_markup=reply_markup
         )

      except Exception as e:
          logger.error(f"Error getting container status for user {user_id}: {e}")
          await query.edit_message_text(
              f"❌ Ошибка при получении статуса контейнера: {str(e)}",
              reply_markup=InlineKeyboardMarkup([
                  [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
              ])
          )

    async def stop_session(self, update: Update, context: CallbackContext):
        """Останавливает текущую сессию"""
        query = update.callback_query
        user_id = query.from_user.id

        # Останавливаем все команды пользователя
        await self.cancel_user_commands(user_id)

        # Удаляем сессию и контейнер
        session_key = f"session:{user_id}"
        session_data = self.redis.get(session_key)

        if session_data:
            try:
                session = json.loads(session_data)
                container_id = session.get('container_id')
                if container_id:
                    try:
                        container = self.docker_client.containers.get(container_id)
                        logger.info(f"Stopping container {container_id} for user {user_id}")
                        container.stop()
                        container.remove()
                        logger.info(f"Остановлен контейнер: {container_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при остановке контейнера: {e}")
            except Exception as e:
                logger.error(f"Ошибка при обработке сессии: {e}")

        self.redis.delete(session_key)
        logger.info(f"Session stopped for user {user_id}")

        keyboard = [
            [InlineKeyboardButton("🚀 Запустить конвейер", callback_data=f"launch:{user_id}")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ Конвейер остановлен и контейнер удален.",
            reply_markup=reply_markup
        )

    async def admin_token_management(self, update: Update, context: CallbackContext):
        """Управление токенами пользователей для администраторов"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        # Получаем статистику по токенам
        token_keys = self.redis.keys("tokens:*")
        users_with_tokens = []

        for key in token_keys:
            user_id_str = key.split(":")[1]
            tokens = self.redis.get(key)
            users_with_tokens.append((user_id_str, int(tokens)))

        users_list = "\n".join([f"• {user_id}: {tokens} токенов" for user_id, tokens in users_with_tokens]) if users_with_tokens else "• Нет пользователей с токенами"

        keyboard = [
            [InlineKeyboardButton("➕ Пополнить токены", callback_data=f"admin_add_tokens:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"admin:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🎫 Управление токенами\n\n"
            f"📊 Пользователи с токенами:\n{users_list}\n\n"
            f"Выберите действие:",
            reply_markup=reply_markup
        )

    async def cancel_user_commands(self, user_id):
        """Отменяет все команды пользователя"""
        if user_id in self.command_workers:
            self.command_workers[user_id].cancel()
            del self.command_workers[user_id]
            logger.info(f"Cancelled command worker for user {user_id}")

        if user_id in self.command_queues:
            # Очищаем очередь
            while not self.command_queues[user_id].empty():
                try:
                    self.command_queues[user_id].get_nowait()
                    self.command_queues[user_id].task_done()
                except:
                    break
            del self.command_queues[user_id]
            logger.info(f"Cleared command queue for user {user_id}")

        if user_id in self.active_commands:
            del self.active_commands[user_id]
            logger.info(f"Removed active commands for user {user_id}")

    async def cancel_command(self, update: Update, context: CallbackContext):
        """Обработчик команды /cancel - отменяет все команды пользователя"""
        user_id = update.effective_user.id

        await self.cancel_user_commands(user_id)

        await update.message.reply_text(
            "✅ Все ваши команды отменены, очередь очищена."
        )

    async def launch_menu(self, update: Update, context: CallbackContext):
        """Меню запуска конвейера с учетом токенов"""
        query = update.callback_query
        user_id = query.from_user.id
        is_confirmed = self.is_confirmed_user(user_id)

        # Инициализируем токены для нового пользователя
        if not is_confirmed:
            self.init_user_tokens(user_id)

        # Проверяем токены для неподтвержденных пользователей
        user_tokens = self.get_user_tokens(user_id) if not is_confirmed else None
        has_tokens = user_tokens > 0 if user_tokens is not None else True

        keyboard = []

        if is_confirmed or has_tokens:
            # Показываем обычные образы
            row = []
            for image_key, image_name in self.available_images.items():
                # Проверяем доступность образов
                if image_key == "archlinux:latest" and not is_confirmed:
                    continue  # Пропускаем Arch для неподтвержденных

                button = InlineKeyboardButton(f"🐧 {image_name}", callback_data=f"image:{user_id}:{image_key}")
                row.append(button)

                if len(row) == 2:
                    keyboard.append(row)
                    row = []

            if row:
                keyboard.append(row)

            # Добавляем опцию кастомного образа для подтвержденных пользователей
            if is_confirmed:
                keyboard.append([InlineKeyboardButton("📝 Кастомный образ", callback_data=f"custom:{user_id}")])

        # Добавляем тестовую конфигурацию для всех
        keyboard.append([InlineKeyboardButton("🧪 Тестовая конфигурация", callback_data=f"image:{user_id}:test")])

        # Кнопка назад
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"main:{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Формируем текст с информацией о токенах
        status_text = "✅ Подтвержденный пользователь" if is_confirmed else "⏳ Обычный пользователь"
        tokens_text = f"\n🎫 Доступно токенов: {user_tokens}" if not is_confirmed and has_tokens else ""
        no_tokens_text = "\n🔴 Токены закончились - доступен только тестовый режим" if not is_confirmed and not has_tokens else ""

        await query.edit_message_text(
            f"🚀 Запуск конвейера\n\n"
            f"Статус: {status_text}{tokens_text}{no_tokens_text}\n\n"
            "Выберите образ системы:",
            reply_markup=reply_markup
        )

    async def select_image(self, update: Update, context: CallbackContext, image_key: str):
        """Обработка выбора образа, включая тестовую конфигурацию"""
        query = update.callback_query
        user_id = query.from_user.id

        # Проверяем токены для неподтвержденных пользователей
        if not self.is_confirmed_user(user_id) and image_key != "test":
            user_tokens = self.get_user_tokens(user_id)
            if user_tokens <= 0:
                await query.answer("❌ Токены закончились! Используйте тестовую конфигурацию.", show_alert=True)
                return

        if image_key == "test":
            # Тестовая конфигурация - сразу создаем контейнер
            await query.edit_message_text("⏳ Создаю тестовый контейнер...")

            try:
                container = await self.create_user_container(
                    user_id,
                    self.test_config["image"],  # Используем образ из конфигурации
                    self.test_config["shell"],  # Используем шелл из конфигурации
                    self.test_config["max_session_time"],  # 20 минут
                    "20m",
                    "test",  # config_key
                    True,  # network
                    True   # is_test
                )

                keyboard = [
                    [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    f"🧪 Тестовый контейнер запущен!\n\n"
                    f"🐧 Образ: {self.available_images.get(self.test_config['image'], self.test_config['image'])}\n"
                    f"💻 Шелл: {self.test_config['shell']}\n"
                    f"⚙️ Конфигурация: Тестовая\n"
                    f"⏰ Время жизни: 20 минут\n"
                    f"⏱ Таймаут команд: {self.test_config['timeout']} секунд\n\n"
                    f"⚠️ Ограничения:\n"
                    f"• Нельзя запускать команды в фоне\n"
                    f"• Ограниченные ресурсы ({self.test_config['mem_limit']} RAM, {self.test_config['cpu_quota']/1000}% CPU)\n\n"
                    f"Теперь вы можете отправлять команды для выполнения в контейнере.",
                    reply_markup=reply_markup
                )

            except Exception as e:
                logger.error(f"Error creating test container: {e}")
                await query.edit_message_text(f"❌ Ошибка при создании тестового контейнера: {str(e)}")

            return  # Важно: выходим из метода после создания тестового контейнера

        # Обычная обработка выбора образа
        context.user_data['selected_image'] = image_key

        # Теперь предлагаем выбрать шелл
        keyboard = []

        for shell in self.available_shells:
            button = InlineKeyboardButton(f"💻 {shell}", callback_data=f"shell:{query.from_user.id}:{shell}")
            keyboard.append([button])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"launch:{query.from_user.id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        image_name = self.available_images.get(image_key, image_key)

        await query.edit_message_text(
            f"🐧 Выбран образ: {image_name}\n\n"
            "Теперь выберите шелл:",
            reply_markup=reply_markup
        )

    async def custom_image_input(self, update: Update, context: CallbackContext):
        """Запрос кастомного образа"""
        query = update.callback_query
        user_id = query.from_user.id

        await query.edit_message_text(
            "📝 Кастомный образ\n\n"
            "Введите имя Docker образа (например: python:3.9, node:18, nginx:latest):\n\n"
            "Или отправьте /cancel для отмены."
        )

        # Сохраняем состояние для ConversationHandler
        context.user_data['waiting_for_custom_image'] = True
        return CUSTOM_IMAGE

    async def process_custom_image(self, update: Update, context: CallbackContext):
        """Обработка введенного кастомного образа"""
        custom_image = update.message.text.strip()
        user_id = update.effective_user.id
        context.user_data['selected_image'] = custom_image
        context.user_data['waiting_for_custom_image'] = False

        # Предлагаем выбрать шелл
        keyboard = []

        for shell in self.available_shells:
            button = InlineKeyboardButton(f"💻 {shell}", callback_data=f"shell:{user_id}:{shell}")
            keyboard.append([button])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"launch:{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🐧 Выбран образ: {custom_image}\n\n"
            "Теперь выберите шелл:",
            reply_markup=reply_markup
        )

        return ConversationHandler.END

    async def select_shell(self, update: Update, context: CallbackContext, shell: str):
        """Обработка выбора шелла и переход к выбору конфигурации"""
        query = update.callback_query
        user_id = query.from_user.id

        if shell not in self.available_shells:
            await query.edit_message_text(f"❌ Ошибка: неизвестный шелл {shell}")
            return

        context.user_data['selected_shell'] = shell

        # Теперь предлагаем выбрать конфигурацию ресурсов
        is_confirmed = self.is_confirmed_user(user_id)
        is_admin = self.is_admin(user_id)

        keyboard = []

        for config_key, config in self.resource_configs.items():
            # Проверяем доступность конфигураций
            if config_key == "medium" and not is_confirmed:
                continue
            if config_key == "enhanced" and not is_confirmed:
                continue
            if config_key == "maximum" and not is_admin:
                continue

            button = InlineKeyboardButton(
                f"⚙️ {config['name']} ({config['description']})",
                callback_data=f"config:{user_id}:{config_key}"
            )
            keyboard.append([button])

        # Добавляем переключатель сети
        network_status = context.user_data.get('network', True)
        network_text = "🌐 Сеть: ВКЛ" if network_status else "🚫 Сеть: ВЫКЛ"
        network_callback = f"network:{user_id}:{'false' if network_status else 'true'}"
        keyboard.append([InlineKeyboardButton(network_text, callback_data=network_callback)])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"launch:{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        image_key = context.user_data.get('selected_image', 'alpine:latest')
        image_name = self.available_images.get(image_key, image_key)

        await query.edit_message_text(
            f"🚀 Настройка конвейера\n\n"
            f"🐧 Образ: {image_name}\n"
            f"💻 Шелл: {shell}\n\n"
            f"⚙️ Выберите конфигурацию ресурсов:\n\n"
            f"Доступные конфигурации зависят от вашего статуса.",
            reply_markup=reply_markup
        )

    async def toggle_network(self, update: Update, context: CallbackContext, network_state: bool):
        """Переключает состояние сети"""
        query = update.callback_query
        context.user_data['network'] = network_state

        # Возвращаемся к выбору конфигурации
        shell = context.user_data.get('selected_shell', 'bash')
        await self.select_shell(update, context, shell)

    async def select_config(self, update: Update, context: CallbackContext, config_key: str):
        """Обработка выбора конфигурации и переход к выбору TTL"""
        query = update.callback_query
        user_id = query.from_user.id

        if config_key not in self.resource_configs:
            await query.edit_message_text("❌ Ошибка: неизвестная конфигурация")
            return

        context.user_data['selected_config'] = config_key

        # Теперь предлагаем выбрать время жизни
        is_confirmed = self.is_confirmed_user(user_id)
        is_admin = self.is_admin(user_id)

        keyboard = []
        row = []

        for ttl_name, ttl_seconds in self.ttl_options.items():
            # Для неподтвержденных пользователей ограничиваем максимальное время
            if not is_confirmed and ttl_seconds and ttl_seconds > 86400:  # 24 часа
                continue

            # "Всегда" только для админов
            if ttl_name == "always" and not is_admin:
                continue

            button = InlineKeyboardButton(f"⏰ {ttl_name}", callback_data=f"ttl:{user_id}:{ttl_name}")
            row.append(button)

            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"launch:{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        image_key = context.user_data.get('selected_image', 'alpine:latest')
        image_name = self.available_images.get(image_key, image_key)
        config_name = self.resource_configs[config_key]['name']
        network = context.user_data.get('network', True)

        max_ttl_text = "12 дней" if is_confirmed else "24 часа"

        await query.edit_message_text(
            f"🚀 Настройка конвейера\n\n"
            f"🐧 Образ: {image_name}\n"
            f"💻 Шелл: {context.user_data.get('selected_shell', 'bash')}\n"
            f"⚙️ Конфигурация: {config_name}\n"
            f"🌐 Сеть: {'включена' if network else 'выключена'}\n\n"
            f"⏰ Сколько будет жить ваш конвейер?\n"
            f"Максимум для вашего статуса: {max_ttl_text}\n\n"
            f"Выберите время жизни:",
            reply_markup=reply_markup
        )

    async def select_ttl(self, update: Update, context: CallbackContext, ttl_name: str):
        """Обработка выбора TTL и запуск контейнера"""
        query = update.callback_query
        user_id = query.from_user.id

        if ttl_name not in self.ttl_options:
            await query.edit_message_text("❌ Ошибка: неизвестное время жизни")
            return

        ttl_seconds = self.ttl_options[ttl_name]

        shell = context.user_data.get('selected_shell', 'bash')
        image_key = context.user_data.get('selected_image', 'alpine:latest')
        config_key = context.user_data.get('selected_config', 'minimal')
        network = context.user_data.get('network', True)

        # Запускаем контейнер
        try:
            await query.edit_message_text("⏳ Создаю контейнер...")

            container = await self.create_user_container(
                user_id, image_key, shell, ttl_seconds, ttl_name,
                config_key, network
            )

            # Отправляем информацию о запущенном контейнере
            image_name = self.available_images.get(image_key, image_key)
            ttl_display = "всегда" if ttl_name == "always" else ttl_name
            config_name = self.resource_configs[config_key]['name']

            keyboard = [
                [InlineKeyboardButton("🔙 Главное меню", callback_data=f"main:{user_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"✅ Контейнер запущен!\n\n"
                f"🐧 Образ: {image_name}\n"
                f"💻 Шелл: {shell}\n"
                f"⚙️ Конфигурация: {config_name}\n"
                f"🌐 Сеть: {'включена' if network else 'выключена'}\n"
                f"⏰ Время жизни: {ttl_display}\n\n"
                f"Теперь вы можете отправлять команды для выполнения в контейнере.",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Error creating container: {e}")
            await query.edit_message_text(f"❌ Ошибка при создании контейнера: {str(e)}")

    async def create_user_container(self, user_id, image, shell="bash", ttl_seconds=None, ttl_display="unknown",
                                config_key="minimal", network=True, is_test=False):
        """Создает контейнер для пользователя с поддержкой тестового режима"""
        # Проверяем, есть ли уже контейнер
        session_key = f"session:{user_id}"
        session_data = self.redis.get(session_key)

        if session_data:
            try:
                session = json.loads(session_data)
                container_id = session.get('container_id')
                if container_id:
                    try:
                        container = self.docker_client.containers.get(container_id)
                        # Останавливаем старый контейнер
                        logger.info(f"Stopping old container {container_id} for user {user_id}")
                        container.stop()
                        container.remove()
                        logger.info(f"Removed old container {container_id}")
                    except Exception as e:
                        logger.warning(f"Could not remove old container: {e}")
            except Exception as e:
                logger.error(f"Error processing old session: {e}")

        # Получаем конфигурацию ресурсов
        if is_test:
            # Используем тестовую конфигурацию
            config = {
                "name": "Тестовая",
                "cpu_period": self.test_config["cpu_period"],
                "cpu_quota": self.test_config["cpu_quota"],
                "mem_limit": self.test_config["mem_limit"],
                "pids_limit": self.test_config["pids_limit"],
                "description": "50MB RAM, 25% CPU"
            }
        else:
            config = self.resource_configs[config_key]

        # Создаем новый контейнер
        container_kwargs = {
            "image": image,
            "command": f"tail -f /dev/null",
            "name": f"terminal_bot_{user_id}_{os.urandom(4).hex()}",
            "network_mode": "bridge" if network else "none",
            "read_only": False,
            "detach": True,
            "tty": True
        }

        # Добавляем ограничения ресурсов
        container_kwargs.update({
            "mem_limit": config["mem_limit"],
            "cpu_period": config["cpu_period"],
            "cpu_quota": config["cpu_quota"],
            "pids_limit": config["pids_limit"]
        })

        # Для неподтвержденных пользователей добавляем дополнительные ограничения
        if not self.is_confirmed_user(user_id) and not is_test:
            container_kwargs.update({
                "mem_limit": "64m",  # Фиксированный лимит для неподтвержденных
                "pids_limit": 20
            })

        logger.info(f"Creating container for user {user_id} with image {image}, is_test: {is_test}")
        container = self.docker_client.containers.run(**container_kwargs)

        # Сохраняем информацию о сессии
        session_data = {
            'container_id': container.id,
            'image': image,
            'shell': shell,
            'ttl_seconds': ttl_seconds,
            'ttl_display': ttl_display,
            'config_name': config["name"],
            'network': network,
            'created_at': datetime.now().isoformat(),
            'is_confirmed': self.is_confirmed_user(user_id),
            'is_test': is_test
        }

        # Устанавливаем TTL если указано
        if ttl_seconds:
            self.redis.setex(session_key, ttl_seconds, json.dumps(session_data))
        else:
            self.redis.set(session_key, json.dumps(session_data))

        # Запускаем потребление токенов для обычных контейнеров
        if not is_test and not self.is_confirmed_user(user_id):
            await self.start_token_consumption(user_id, container.id)

        logger.info(f"Created container {container.id} for user {user_id} with image {image}, shell {shell}, TTL: {ttl_display}, is_test: {is_test}")
        return container

    async def _execute_single_command(self, update, context, command, status_msg, user_id):
        """Выполняет команду с таймаутом для тестовых контейнеров"""
        try:
            session_key = f"session:{user_id}"
            session_data = self.redis.get(session_key)

            if not session_data:
                await status_msg.edit_text("❌ Сессия не найдена. Используйте /container для создания новой.")
                return

            session = json.loads(session_data)
            container_id = session.get('container_id')
            shell = session.get('shell', 'bash')
            is_test = session.get('is_test', False)

            if not container_id:
                await status_msg.edit_text("❌ Ошибка: ID контейнера не найден")
                return

            # Получаем контейнер
            try:
                container = self.docker_client.containers.get(container_id)
            except:
                await status_msg.edit_text("❌ Контейнер не найден. Используйте /container для создания нового.")
                self.redis.delete(session_key)
                return

            # Проверка безопасности для неподтвержденных пользователей
            if not self.is_confirmed_user(user_id) and await self.is_command_dangerous(command):
                await status_msg.edit_text("❌ Команда запрещена для выполнения")
                return

            # Выполняем команду через выбранный шелл
            full_command = f"{shell} -c \"{command}\""

            # Выполняем команду в отдельном потоке с таймаутом для тестовых контейнеров
            loop = asyncio.get_event_loop()

            if is_test:
                # Для тестовых контейнеров устанавливаем таймаут
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            self.thread_pool,
                            self._run_command_sync,
                            container,
                            full_command
                        ),
                        timeout=self.test_config["timeout"]
                    )
                except asyncio.TimeoutError:
                    await status_msg.edit_text(f"❌ Таймаут команды ({self.test_config['timeout']} секунд)")
                    return
            else:
                # Обычные контейнеры без таймаута
                result = await loop.run_in_executor(
                    self.thread_pool,
                    self._run_command_sync,
                    container,
                    full_command
                )

            output, exit_code = result

            if exit_code != 0 and not output:
                output = f"Команда завершилась с кодом ошибки: {exit_code}"
            elif not output:
                output = "Команда выполнена успешно (нет вывода)"

            await self.send_smart_output(status_msg, output, exit_code)

        except Exception as e:
            logger.error(f"Error executing command for user {user_id}: {e}")
            try:
                await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
            except:
                pass

    def _run_command_sync(self, container, full_command):
        """Синхронное выполнение команды (вызывается в отдельном потоке)"""
        try:
            result = container.exec_run(
                full_command,
                stdout=True,
                stderr=True,
                stdin=False
            )

            output = result.output.decode('utf-8', errors='ignore') if result.output else ""
            exit_code = result.exit_code

            return output, exit_code
        except Exception as e:
            logger.error(f"Error in sync command execution: {e}")
            return f"Ошибка выполнения: {str(e)}", 1

    async def execute_command(self, update: Update, context: CallbackContext):
        """Добавляет команду в очередь пользователя"""
        user_id = update.effective_user.id
        command = update.message.text.strip()

        # Проверяем, есть ли активная сессия
        if not self.has_active_session(user_id):
            await update.message.reply_text(
                "❌ У вас нет активного контейнера. Используйте /container для создания нового."
            )
            return

        # Запускаем воркер если нужно
        if user_id not in self.command_workers:
            await self.start_command_worker(user_id)

        # Создаем очередь если нужно
        if user_id not in self.command_queues:
            self.command_queues[user_id] = asyncio.Queue()

        # Отправляем сообщение о выполнении
        status_msg = await update.message.reply_text("⏳ Команда добавлена в очередь...")

        # Добавляем команду в очередь
        await self.command_queues[user_id].put((update, context, command, status_msg))

        # Обновляем статус
        queue_size = self.command_queues[user_id].qsize()
        await status_msg.edit_text(f"⏳ Команда в очереди... (позиция: {queue_size})")

    async def docker_command(self, update: Update, context: CallbackContext):
        """Обработка команд с префиксом /docker в группах"""
        user_id = update.effective_user.id

        # Проверяем, есть ли аргументы
        if not context.args:
            await update.message.reply_text(
                "Использование: /docker <команда>\n\n"
                "Примеры:\n"
                "/docker ls\n"
                "/docker pwd\n"
                "/docker apt update"
            )
            return

        command = ' '.join(context.args)

        # Проверяем, есть ли активная сессия
        if not self.has_active_session(user_id):
            await update.message.reply_text(
                "❌ У вас нет активного контейнера. Используйте /container в личном чате с ботом для создания контейнера."
            )
            return

        # Запускаем воркер если нужно
        if user_id not in self.command_workers:
            await self.start_command_worker(user_id)

        # Создаем очередь если нужно
        if user_id not in self.command_queues:
            self.command_queues[user_id] = asyncio.Queue()

        # Отправляем сообщение о выполнении
        status_msg = await update.message.reply_text(f"⏳ Команда для @{update.effective_user.username or update.effective_user.first_name} добавлена в очередь...")

        # Добавляем команду в очередь
        await self.command_queues[user_id].put((update, context, command, status_msg))

        # Обновляем статус
        queue_size = self.command_queues[user_id].qsize()
        await status_msg.edit_text(f"⏳ Команда в очереди... (позиция: {queue_size})")

    async def send_smart_output(self, message, output: str, exit_code: int):
        """Умная отправка вывода с форматированием"""
        try:
            if len(output) > 4000:
                output = output[:4000] + "\n... (вывод обрезан)"

            status_icon = "✅" if exit_code == 0 else "❌"
            status_text = "успешно" if exit_code == 0 else f"ошибка (код: {exit_code})"

            await message.edit_text(
                f"{status_icon} Команда выполнена {status_text}:\n```\n{output}\n```",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error in send_smart_output: {e}")
            try:
                if len(output) > 4000:
                    output = output[:4000] + "\n... (вывод обрезан)"
                await message.edit_text(f"```\n{output}\n```", parse_mode='Markdown')
            except Exception as e2:
                logger.error(f"Error sending plain text: {e2}")
                await message.edit_text("❌ Ошибка при отправке вывода")

    async def handle_upload(self, update: Update, context: CallbackContext):
        """Простая и надежная загрузка файлов"""
        user_id = update.effective_user.id

        if not self.has_active_session(user_id):
            await update.message.reply_text("❌ Нет активного контейнера")
            return

        if not update.message.document:
            await update.message.reply_text("❌ Пожалуйста, отправьте файл как документ")
            return

        document = update.message.document
        file_size = document.file_size
        file_name = document.file_name or "uploaded_file"

        # Проверяем лимиты
        is_confirmed = self.is_confirmed_user(user_id)
        user_type = 'confirmed' if is_confirmed else 'unconfirmed'
        max_upload = self.file_limits[user_type]['upload']

        if file_size > max_upload:
            await update.message.reply_text(
                f"❌ Файл слишком большой! Максимум: {max_upload // (1024 * 1024)} МБ"
            )
            return

        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')

        try:
            container = self.docker_client.containers.get(container_id)
        except:
            await update.message.reply_text("❌ Контейнер не найден")
            return

        status_msg = await update.message.reply_text("⏳ Загружаю файл...")

        try:
            # Получаем файл от Telegram
            file = await context.bot.get_file(document.file_id)

            # Создаем временную директорию
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_path = os.path.join(temp_dir, file_name)

                # Скачиваем файл
                await file.download_to_drive(temp_file_path)

                # Создаем tar архив
                tar_buffer = io.BytesIO()
                with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
                    tar.add(temp_file_path, arcname=file_name)
                tar_buffer.seek(0)

                # Копируем в корень контейнера
                container.put_archive(path='/', data=tar_buffer.read())

                # Проверяем что файл загружен
                loop = asyncio.get_event_loop()
                check_result = await loop.run_in_executor(
                    self.thread_pool,
                    self._run_command_sync,
                    container,
                    f"test -f /{file_name} && echo 'SUCCESS' || echo 'FAILED'"
                )

                if "SUCCESS" in check_result[0]:
                    await status_msg.edit_text(
                        f"✅ Файл загружен!\n\n"
                        f"📁 Имя: `{file_name}`\n"
                        f"📊 Размер: {file_size // 1024} КБ\n"
                        f"📍 Расположение: `/` (корневая директория)\n\n"
                        f"💡 Чтобы переместить в текущую директорию:\n"
                        f"`mv /{file_name} ./`",
                        parse_mode='Markdown'
                    )
                else:
                    await status_msg.edit_text("❌ Не удалось загрузить файл")

        except Exception as e:
            logger.error(f"Error uploading file for user {user_id}: {e}")
            await status_msg.edit_text(f"❌ Ошибка: {str(e)}")

    async def handle_upload(self, update: Update, context: CallbackContext):
        """Простая и надежная загрузка файлов"""
        user_id = update.effective_user.id

        if not self.has_active_session(user_id):
            await update.message.reply_text("❌ Нет активного контейнера")
            return

        if not update.message.document:
            await update.message.reply_text("❌ Пожалуйста, отправьте файл как документ")
            return

        document = update.message.document
        file_size = document.file_size
        file_name = document.file_name or "uploaded_file"

        # Проверяем лимиты
        is_confirmed = self.is_confirmed_user(user_id)
        user_type = 'confirmed' if is_confirmed else 'unconfirmed'
        max_upload = self.file_limits[user_type]['upload']

        if file_size > max_upload:
            await update.message.reply_text(
                f"❌ Файл слишком большой! Максимум: {max_upload // (1024 * 1024)} МБ"
            )
            return

        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')

        try:
            container = self.docker_client.containers.get(container_id)
        except:
            await update.message.reply_text("❌ Контейнер не найден")
            return

        status_msg = await update.message.reply_text("⏳ Загружаю файл...")

        try:
            # Получаем файл от Telegram
            file = await context.bot.get_file(document.file_id)

            # Создаем временную директорию
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_path = os.path.join(temp_dir, file_name)

                # Скачиваем файл
                await file.download_to_drive(temp_file_path)

                # Создаем tar архив
                tar_buffer = io.BytesIO()
                with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
                    tar.add(temp_file_path, arcname=file_name)
                tar_buffer.seek(0)

                # Копируем в корень контейнера
                container.put_archive(path='/', data=tar_buffer.read())

                # Проверяем что файл загружен
                loop = asyncio.get_event_loop()
                check_result = await loop.run_in_executor(
                    self.thread_pool,
                    self._run_command_sync,
                    container,
                    f"test -f /{file_name} && echo 'SUCCESS' || echo 'FAILED'"
                )

                if "SUCCESS" in check_result[0]:
                    await status_msg.edit_text(
                        f"✅ Файл загружен!\n\n"
                        f"📁 Имя: `{file_name}`\n"
                        f"📊 Размер: {file_size // 1024} КБ\n"
                        f"📍 Расположение: `/` (корневая директория)\n\n"
                        f"💡 Чтобы переместить в текущую директорию:\n"
                        f"`mv /{file_name} ./`",
                        parse_mode='Markdown'
                    )
                else:
                    await status_msg.edit_text("❌ Не удалось загрузить файл")

        except Exception as e:
            logger.error(f"Error uploading file for user {user_id}: {e}")
            await status_msg.edit_text(f"❌ Ошибка: {str(e)}")

    async def handle_download(self, update: Update, context: CallbackContext):
        """Простая и надежная выгрузка файлов"""
        user_id = update.effective_user.id

        if not context.args:
            await update.message.reply_text(
                "Использование: /download <путь_к_файлу>\n\n"
                "Примеры:\n"
                "/download /home/user/file.txt\n"
                "/download ./script.py\n"
                "/download /tmp/data.json"
            )
            return

        file_path = ' '.join(context.args)

        if not self.has_active_session(user_id):
            await update.message.reply_text("❌ Нет активного контейнера")
            return

        session_info = self.get_session_info(user_id)
        container_id = session_info.get('container_id')

        try:
            container = self.docker_client.containers.get(container_id)
        except:
            await update.message.reply_text("❌ Контейнер не найден")
            return

        status_msg = await update.message.reply_text("⏳ Проверяю файл...")

        try:
            loop = asyncio.get_event_loop()

            # Проверяем существование файла
            check_result = await loop.run_in_executor(
                self.thread_pool,
                self._run_command_sync,
                container,
                f"test -f '{file_path}' && stat -c%s '{file_path}' || echo 'NOT_FOUND'"
            )

            if "NOT_FOUND" in check_result[0]:
                await status_msg.edit_text(f"❌ Файл не найден: `{file_path}`", parse_mode='Markdown')
                return

            # Получаем размер файла
            file_size_str = check_result[0].strip()
            if file_size_str == "NOT_FOUND":
                await status_msg.edit_text(f"❌ Файл не найден: `{file_path}`", parse_mode='Markdown')
                return

            file_size = int(file_size_str)

            # Проверяем лимиты
            is_confirmed = self.is_confirmed_user(user_id)
            user_type = 'confirmed' if is_confirmed else 'unconfirmed'
            max_download = self.file_limits[user_type]['download']

            if file_size > max_download:
                await status_msg.edit_text(
                    f"❌ Файл слишком большой! Максимум: {max_download // (1024 * 1024)} МБ\n"
                    f"Размер файла: {file_size // (1024 * 1024)} МБ"
                )
                return

            # Получаем имя файла
            name_result = await loop.run_in_executor(
                self.thread_pool,
                self._run_command_sync,
                container,
                f"basename '{file_path}'"
            )

            file_name = name_result[0].strip() if name_result[0] else "download_file"

            await status_msg.edit_text("⏳ Подготавливаю файл...")

            # Создаем временную директорию
            with tempfile.TemporaryDirectory() as temp_dir:
                # Получаем файл из контейнера
                bits, stat = container.get_archive(file_path)

                # Сохраняем tar архив
                tar_path = os.path.join(temp_dir, "download.tar")
                with open(tar_path, 'wb') as f:
                    for chunk in bits:
                        f.write(chunk)

                # Извлекаем файл
                extracted_path = os.path.join(temp_dir, file_name)
                with tarfile.open(tar_path, 'r') as tar:
                    # Извлекаем первый файл из архива
                    members = tar.getmembers()
                    if members:
                        tar.extract(members[0], temp_dir)
                        # Переименовываем если нужно
                        old_path = os.path.join(temp_dir, members[0].name)
                        if os.path.exists(old_path) and old_path != extracted_path:
                            os.rename(old_path, extracted_path)

                # Отправляем файл
                if os.path.exists(extracted_path):
                    with open(extracted_path, 'rb') as f:
                        await update.message.reply_document(
                            document=f,
                            filename=file_name,
                            caption=f"📁 Файл: `{file_path}`\n📊 Размер: {file_size // 1024} КБ",
                            parse_mode='Markdown'
                        )
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ Не удалось извлечь файл из архива")

        except Exception as e:
            logger.error(f"Error downloading file for user {user_id}: {e}")
            await status_msg.edit_text(f"❌ Ошибка: {str(e)}")


    async def information_menu(self, update: Update, context: CallbackContext):
        """Меню информации"""
        query = update.callback_query
        user_id = query.from_user.id
        is_confirmed = self.is_confirmed_user(user_id)

        # Определяем user_type для всех случаев
        user_type = 'confirmed' if is_confirmed else 'unconfirmed'

        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data=f"main:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        status_text = "✅ Подтвержденный пользователь" if is_confirmed else "⏳ Обычный пользователь"

        await query.edit_message_text(
            f"ℹ️ Информация о боте\n\n"
            f"🤖 Terminal Bot - безопасный Docker-терминал\n\n"
            f"📊 Ваш статус: {status_text}\n\n"
            f"🎫 Система токенов:\n"
            f"• Подтвержденные: безлимитный доступ\n"
            f"• Неподтвержденные: {self.initial_tokens} начальных токенов\n"
            f"• Расход: 1 токен/минута за обычные контейнеры\n"
            f"• Тестовые контейнеры: бесплатно\n\n"
            f"📁 Работа с файлами:\n"
            f"• Лимиты: {self.file_limits[user_type]['upload'] // (1024*1024)}МБ / {self.file_limits[user_type]['download'] // (1024*1024)}МБ\n\n"
            f"🐧 Доступные образы:\n"
            f"• Alpine, Ubuntu, Debian, Kali, openSUSE, Fedora\n"
            f"• Arch Linux (только для подтвержденных)\n"
            f"• Кастомные образы (только для подтвержденных)\n"
            f"• Тестовый Alpine (бесплатно для всех)\n\n"
            f"💻 Доступные шеллы: bash, sh\n\n"
            f"⏰ Время сеанса:\n"
            f"• Подтвержденные: до 12 дней\n"
            f"• Неподтвержденные: до 24 часов\n"
            f"• Тестовые контейнеры: 20 минут\n"
            f"• Администраторы: бессрочно\n\n"
            f"💬 Использование в группах:\n"
            f"• /docker <команда> - выполнить команду\n"
            f"• /docker - справка\n\n"
            f"⚡ Основные команды:\n"
            f"• /container - управление контейнерами\n"
            f"• /download <путь> - выгрузить файл\n"
            f"• /nohup <команда> - запустить в фоне\n"
            f"• /processes - показать процессы\n"
            f"• /state - текущее состояние",
            reply_markup=reply_markup
        )

    async def admin_menu(self, update: Update, context: CallbackContext):
        """Админ панель"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа к админ панели")
            return

        keyboard = [
            [InlineKeyboardButton("👥 Управление пользователями", callback_data=f"user_manage:{user_id}")],
            [InlineKeyboardButton("🐳 Управление контейнерами", callback_data=f"container_manage:{user_id}")],
            [InlineKeyboardButton("📊 Статистика", callback_data=f"admin_stats:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"main:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "👑 Админ панель\n\n"
            "Выберите раздел для управления:",
            reply_markup=reply_markup
        )

    async def user_management(self, update: Update, context: CallbackContext):
        """Управление пользователями"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        # Получаем список подтвержденных пользователей
        confirmed_users = self.redis.smembers("confirmed_users")

        keyboard = [
            [InlineKeyboardButton("➕ Добавить пользователя", callback_data=f"add_user:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"admin:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        users_list = "\n".join([f"• {user_id}" for user_id in confirmed_users]) if confirmed_users else "• Нет пользователей"

        await query.edit_message_text(
            f"👥 Управление пользователями\n\n"
            f"✅ Подтвержденные пользователи:\n{users_list}\n\n"
            f"Выберите действие:",
            reply_markup=reply_markup
        )

    async def container_management(self, update: Update, context: CallbackContext):
        """Управление контейнерами"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        # Получаем все активные контейнеры
        try:
            containers = self.docker_client.containers.list(
                filters={"name": "terminal_bot_"}
            )

            containers_list = ""
            for container in containers:
                container_name = container.name
                container_status = container.status
                containers_list += f"• {container_name} - {container_status}\n"

            if not containers_list:
                containers_list = "• Нет активных контейнеров"

        except Exception as e:
            logger.error(f"Error getting containers: {e}")
            containers_list = "• Ошибка при получении контейнеров"

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"container_manage:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"admin:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🐳 Управление контейнерами\n\n"
            f"📊 Активные контейнеры:\n{containers_list}",
            reply_markup=reply_markup
        )

    async def admin_stats(self, update: Update, context: CallbackContext):
        """Статистика админ-панели"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        # Получаем статистику
        try:
            # Количество подтвержденных пользователей
            confirmed_users = self.redis.scard("confirmed_users")

            # Количество активных сессий
            active_sessions = 0
            for key in self.redis.keys("session:*"):
                active_sessions += 1

            # Количество активных контейнеров
            active_containers = len(self.docker_client.containers.list(filters={"name": "terminal_bot_"}))

        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            confirmed_users = "Ошибка"
            active_sessions = "Ошибка"
            active_containers = "Ошибка"

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"admin_stats:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data=f"admin:{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"📊 Статистика системы\n\n"
            f"👥 Пользователи:\n"
            f"• Подтвержденных: {confirmed_users}\n"
            f"• Активных сессий: {active_sessions}\n\n"
            f"🐳 Контейнеры:\n"
            f"• Активных: {active_containers}",
            reply_markup=reply_markup
        )

    async def add_user_prompt(self, update: Update, context: CallbackContext):
        """Запрос на добавление пользователя"""
        query = update.callback_query
        user_id = query.from_user.id

        if not self.is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        await query.edit_message_text(
            "👤 Добавление пользователя\n\n"
            "Отправьте ID пользователя для добавления в подтвержденные.\n\n"
            "Чтобы узнать ID пользователя, попросите его отправить /start боту @userinfobot\n\n"
            "Или отправьте /cancel для отмены."
        )

        context.user_data['waiting_for_user_id'] = True
        return CONFIRMING_USER

    async def receive_user_id(self, update: Update, context: CallbackContext):
        """Получение ID пользователя для добавления"""
        user_id_input = update.message.text.strip()
        admin_id = update.effective_user.id

        # Проверяем, является ли ввод числом (ID)
        if not user_id_input.isdigit():
            await update.message.reply_text("❌ ID пользователя должен быть числом. Попробуйте снова или отправьте /cancel")
            return CONFIRMING_USER

        user_id_to_add = int(user_id_input)
        context.user_data['waiting_for_user_id'] = False

        # Создаем клавиатуру для подтверждения
        keyboard = [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_user:{admin_id}:{user_id_to_add}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"user_manage:{admin_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"👤 Подтверждение добавления пользователя\n\n"
            f"ID пользователя: {user_id_to_add}\n\n"
            f"⚠️ Внимание: После подтверждения пользователь получит:\n"
            f"• Доступ к конвейеру навсегда\n"
            f"• Возможность выбирать любой образ (включая Arch Linux)\n"
            f"• Неограниченное время сеанса\n\n"
            f"Вы уверены, что хотите добавить этого пользователя?",
            reply_markup=reply_markup
        )

        return ConversationHandler.END

    async def confirm_add_user(self, update: Update, context: CallbackContext):
        """Подтверждение добавления пользователя"""
        query = update.callback_query
        await query.answer()

        # Извлекаем ID пользователя и админа из callback_data
        callback_data = query.data
        parts = callback_data.split(':')
        admin_id = int(parts[1])
        user_id_to_add = int(parts[2])

        # Проверяем, что нажал тот же админ
        if query.from_user.id != admin_id:
            await query.answer("❌ Это подтверждение не для вас!", show_alert=True)
            return

        if not self.is_admin(admin_id):
            await query.edit_message_text("❌ У вас нет доступа")
            return

        # Добавляем пользователя в подтвержденные
        self.add_confirmed_user(user_id_to_add)

        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data=f"user_manage:{admin_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ Пользователь {user_id_to_add} успешно добавлен в подтвержденные!",
            reply_markup=reply_markup
        )

    async def is_command_dangerous(self, command: str) -> bool:
        """Проверяет команду на опасность для неподтвержденных пользователей"""
        dangerous_patterns = [
            'rm -rf /', 'rm -rf /*', 'dd if=', 'mkfs', ':(){:|:&};:',
            '> /dev/sd', '> /dev/hd', 'chmod 777 /', 'passwd root'
        ]

        command_lower = command.lower()
        return any(pattern in command_lower for pattern in dangerous_patterns)

    async def cancel(self, update: Update, context: CallbackContext):
        """Отмена текущей операции"""
        context.user_data.clear()
        await update.message.reply_text("Операция отменена.")
        return ConversationHandler.END

    async def inline_query(self, update: Update, context: CallbackContext):
        """Обработчик инлайн-запросов - показывает информацию о контейнере пользователя"""
        query = update.inline_query
        user_id = query.from_user.id  # user_id того, кто делает инлайн-запрос

        # Проверяем сессию пользователя, который сделал запрос
        session_info = self.get_session_info(user_id)
        if not session_info:
            results = [
                InlineQueryResultArticle(
                    id='1',
                    title="❌ Нет активного контейнера",
                    description="Сначала создайте контейнер через /container",
                    input_message_content=InputTextMessageContent(
                        "❌ У вас нет активного контейнера. Используйте /container для создания нового."
                    )
                )
            ]
            await update.inline_query.answer(results)
            return

        # Проверяем, существует ли контейнер на самом деле
        container_id = session_info.get('container_id')
        if not container_id:
            # Удаляем невалидную сессию
            session_key = f"session:{user_id}"
            self.redis.delete(session_key)
            results = [
                InlineQueryResultArticle(
                    id='1',
                    title="❌ Нет активного контейнера",
                    description="Сначала создайте контейнер через /container",
                    input_message_content=InputTextMessageContent(
                        "❌ У вас нет активного контейнера. Используйте /container для создания нового."
                    )
                )
            ]
            await update.inline_query.answer(results)
            return

        try:
            # Проверяем, что контейнер действительно существует и работает
            container = self.docker_client.containers.get(container_id)
            if container.status != 'running':
                # Контейнер существует, но не запущен - удаляем сессию
                session_key = f"session:{user_id}"
                self.redis.delete(session_key)
                results = [
                    InlineQueryResultArticle(
                        id='1',
                        title="❌ Контейнер не запущен",
                        description="Создайте новый контейнер через /container",
                        input_message_content=InputTextMessageContent(
                            "❌ Ваш контейнер не запущен. Используйте /container для создания нового."
                        )
                    )
                ]
                await update.inline_query.answer(results)
                return
        except docker.errors.NotFound:
            # Контейнер не найден - удаляем сессию
            session_key = f"session:{user_id}"
            self.redis.delete(session_key)
            results = [
                InlineQueryResultArticle(
                    id='1',
                    title="❌ Контейнер не найден",
                    description="Создайте новый контейнер через /container",
                    input_message_content=InputTextMessageContent(
                        "❌ Ваш контейнер не найден. Используйте /container для создания нового."
                    )
                )
            ]
            await update.inline_query.answer(results)
            return
        except Exception as e:
            logger.error(f"Error checking container in inline query for user {user_id}: {e}")
            results = [
                InlineQueryResultArticle(
                    id='1',
                    title="❌ Ошибка проверки контейнера",
                    description="Попробуйте позже",
                    input_message_content=InputTextMessageContent(
                        "❌ Произошла ошибка при проверке контейнера. Попробуйте позже."
                    )
                )
            ]
            await update.inline_query.answer(results)
            return


        image_name = self.available_images.get(session_info.get('image', ''), session_info.get('image', 'Кастомный'))
        shell = session_info.get('shell', 'bash')
        config_name = session_info.get('config_name', 'Минимальная')
        network = "включена" if session_info.get('network', True) else "выключена"
        ttl = session_info.get('ttl_display', 'Неизвестно')
        created_at = session_info.get('created_at', 'Неизвестно')
        is_test = session_info.get('is_test', False)

        status_text = "🧪 Тестовый" if is_test else "🐳 Обычный"

        text = (f"{status_text} контейнер:\n"
                f"🐧 Образ: {image_name}\n"
                f"💻 Шелл: {shell}\n"
                f"⚙️ Конфигурация: {config_name}\n"
                f"🌐 Сеть: {network}\n"
                f"⏰ Время жизни: {ttl}\n"
                f"📅 Создан: {created_at[:19]}")

        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статус", callback_data=f"status:{user_id}")],
            [InlineKeyboardButton("🔄 Пересоздать", callback_data=f"launch:{user_id}")],
            [InlineKeyboardButton("⏹️ Остановить", callback_data=f"stop:{user_id}")]
        ])

        results = [
            InlineQueryResultArticle(
                id='1',
                title=f"{status_text} контейнер: {image_name}",
                description=f"{shell} | {config_name} | {ttl}",
                input_message_content=InputTextMessageContent(
                    text,
                    parse_mode=None
                ),
                reply_markup=reply_markup
            )
        ]

        await update.inline_query.answer(results)

    async def handle_group_message(self, update: Update, context: CallbackContext):
        """Обработка сообщений в группах"""
        # Игнорируем файлы в группах
        if update.message.document:
            return

        # Обрабатываем только команды /docker
        if update.message.text and update.message.text.startswith('/docker'):
            await self.docker_command(update, context)

def main():
    """Запуск бота"""
    try:
        # Получаем токен из переменной окружения
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            print("❌ Ошибка: TELEGRAM_BOT_TOKEN не установлен")
            print("Установите токен: export TELEGRAM_BOT_TOKEN='ваш_токен'")
            return

        print("✅ Токен получен, запуск бота...")

        bot = TerminalBot()


        application = Application.builder().token(token).build()

        # Универсальный обработчик callback
        application.add_handler(CallbackQueryHandler(bot.handle_callback))

        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bot.custom_image_input, pattern="^custom:"),
                CallbackQueryHandler(bot.add_user_prompt, pattern="^add_user:")
            ],
            states={
                CUSTOM_IMAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.process_custom_image)
                ],
                CONFIRMING_USER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_user_id)
                ]
            },
            fallbacks=[CommandHandler("cancel", bot.cancel)],
            per_message=False
        )

        # Регистрируем обработчики
        application.add_handler(CommandHandler("start", bot.start))
        application.add_handler(CommandHandler("container", bot.container_command))
        application.add_handler(CommandHandler("cancel", bot.cancel_command))
        application.add_handler(CommandHandler("docker", bot.docker_command))
        application.add_handler(CommandHandler("nohup", bot.nohup_command))
        application.add_handler(CommandHandler("processes", bot.background_processes))
        application.add_handler(CommandHandler("kill", bot.kill_process))
        application.add_handler(CommandHandler("download", bot.handle_download))

        # Обработчик загрузки файлов (просто документы в личных чатах)
        application.add_handler(MessageHandler(
            filters.Document.ALL & filters.ChatType.PRIVATE,
            bot.handle_upload
        ))

        # ConversationHandler
        application.add_handler(conv_handler)

        # Инлайн-обработчик
        application.add_handler(InlineQueryHandler(bot.inline_query))

        # Обработчик текстовых сообщений (команды в терминал)
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            bot.execute_command
        ))

        print("🚀 Бот запущен и готов к работе...")
        print("Нажмите Ctrl+C для остановки")

        application.run_polling()

    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")
        logger.error(f"Bot startup error: {e}")

if __name__ == "__main__":
    main()
