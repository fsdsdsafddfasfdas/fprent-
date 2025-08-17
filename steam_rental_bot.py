import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import steam.guard
from FunPayAPI import Account, types
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, CallbackContext

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('steam_rental_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = "8029226459:AAHkgJN-dZXuDF20kB7n5FCdhhbW0yKnu5M"
ADMIN_CHAT_ID = 7890395437
ACCOUNTS_FILE = "accounts.json"
CONFIG_FILE = "config.json"

# Глобальные переменные
funpay_account = None
active_rentals = {}  # chat_id: {login, end_time, api_key, order_id, bonus_given}
user_states = {}  # user_id: {state, data}
pending_contact_messages = set()  # chat_ids ожидающих сообщения для связи

class SteamRentalBot:
    def __init__(self):
        self.updater = Updater(TELEGRAM_TOKEN, use_context=True)
        self.dp = self.updater.dispatcher
        self.setup_handlers()
        self.load_config()
        self.load_accounts()
        
        # Flask для пинга
        self.app = Flask(__name__)
        self.setup_flask()
        
    def setup_handlers(self):
        """Настройка обработчиков Telegram"""
        self.dp.add_handler(CommandHandler("start", self.start_command))
        self.dp.add_handler(CommandHandler("myid", self.myid_command))
        self.dp.add_handler(CommandHandler("set_funpay_token", self.set_funpay_token))
        self.dp.add_handler(CommandHandler("add_account", self.add_account_command))
        self.dp.add_handler(CommandHandler("list_accounts", self.list_accounts))
        self.dp.add_handler(CommandHandler("status", self.status_command))
        self.dp.add_handler(CallbackQueryHandler(self.button_callback))
        self.dp.add_handler(MessageHandler(Filters.text & ~Filters.command, self.handle_message))
        
    def setup_flask(self):
        """Настройка Flask для пинга"""
        @self.app.route('/ping')
        def ping():
            return "OK"
            
    def load_config(self):
        """Загрузка конфигурации"""
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                self.funpay_token = config.get('funpay_token')
        except FileNotFoundError:
            self.funpay_token = None
            
    def save_config(self):
        """Сохранение конфигурации"""
        config = {'funpay_token': self.funpay_token}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            
    def load_accounts(self):
        """Загрузка аккаунтов"""
        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                self.accounts = json.load(f)
        except FileNotFoundError:
            self.accounts = {}
            
    def save_accounts(self):
        """Сохранение аккаунтов"""
        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)
            
    def is_admin(self, user_id: int) -> bool:
        """Проверка админских прав"""
        return user_id == ADMIN_CHAT_ID
        
    def start_command(self, update: Update, context: CallbackContext):
        """Команда /start"""
        if not self.is_admin(update.effective_user.id):
            return
            
        update.message.reply_text("👋 Бот запущен! Используйте /myid для chat_id.")
        logger.info("Бот запущен администратором")
        
    def myid_command(self, update: Update, context: CallbackContext):
        """Команда /myid"""
        chat_id = update.effective_chat.id
        update.message.reply_text(f"🆔 Ваш chat_id: {chat_id}")
        
    def set_funpay_token(self, update: Update, context: CallbackContext):
        """Команда /set_funpay_token"""
        if not self.is_admin(update.effective_user.id):
            return
            
        if not context.args:
            update.message.reply_text("❌ Формат: /set_funpay_token <token>")
            return
            
        self.funpay_token = context.args[0]
        self.save_config()
        update.message.reply_text("✅ FunPay токен установлен. Перезапустите бота.")
        logger.info("FunPay токен установлен")
        
    def add_account_command(self, update: Update, context: CallbackContext):
        """Команда /add_account"""
        if not self.is_admin(update.effective_user.id):
            return
            
        user_id = update.effective_user.id
        user_states[user_id] = {'state': 'waiting_login', 'data': {}}
        
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        update.message.reply_text("📝 Введите логин Steam:", reply_markup=reply_markup)
        
    def list_accounts(self, update: Update, context: CallbackContext):
        """Команда /list_accounts"""
        if not self.is_admin(update.effective_user.id):
            return
            
        if not self.accounts:
            update.message.reply_text("📋 Пусто.")
            return
            
        message = "📋 Аккаунты:\n"
        for login, data in self.accounts.items():
            games = ", ".join(data.get('games', []))
            status = "🟢 Свободен" if data.get('status') == 'free' else "🔴 Занят"
            message += f"{login}: {games} ({status})\n"
            
        update.message.reply_text(message)
        
    def status_command(self, update: Update, context: CallbackContext):
        """Команда /status"""
        if not self.is_admin(update.effective_user.id):
            return
            
        if not active_rentals:
            update.message.reply_text("📊 Нет активных аренд.")
            return
            
        message = "📊 Активные аренды:\n"
        for chat_id, rental in active_rentals.items():
            remaining = max(0, int((rental['end_time'] - time.time()) / 60))
            message += f"Чат {chat_id}: {rental['login']}, осталось {remaining} мин\n"
            
        update.message.reply_text(message)
        
    def button_callback(self, update: Update, context: CallbackContext):
        """Обработка нажатий кнопок"""
        query = update.callback_query
        query.answer()
        
        if query.data == "cancel_add":
            user_id = query.from_user.id
            if user_id in user_states:
                del user_states[user_id]
            query.edit_message_text("❌ Добавление аккаунта отменено.")
            
    def handle_message(self, update: Update, context: CallbackContext):
        """Обработка текстовых сообщений"""
        if not self.is_admin(update.effective_user.id):
            return
            
        user_id = update.effective_user.id
        text = update.message.text
        
        if user_id not in user_states:
            return
            
        state = user_states[user_id]['state']
        data = user_states[user_id]['data']
        
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if state == 'waiting_login':
            if text in self.accounts:
                update.message.reply_text(f"❌ Аккаунт {text} уже существует.")
                return
            data['login'] = text
            user_states[user_id]['state'] = 'waiting_password'
            update.message.reply_text("🔒 Введите пароль Steam:", reply_markup=reply_markup)
            
        elif state == 'waiting_password':
            data['password'] = text
            user_states[user_id]['state'] = 'waiting_mafile'
            update.message.reply_text("📂 Введите путь к maFile (например, mafiles/login.json):", reply_markup=reply_markup)
            
        elif state == 'waiting_mafile':
            data['mafile_path'] = text
            user_states[user_id]['state'] = 'waiting_games'
            update.message.reply_text("🎮 Введите игры через запятую (например, CS2,Dota2):", reply_markup=reply_markup)
            
        elif state == 'waiting_games':
            games = [game.strip() for game in text.split(',')]
            data['games'] = games
            user_states[user_id]['state'] = 'waiting_api_key'
            update.message.reply_text("🔐 Введите Steam API ключ:", reply_markup=reply_markup)
            
        elif state == 'waiting_api_key':
            data['api_key'] = text
            
            # Сохраняем аккаунт
            login = data['login']
            self.accounts[login] = {
                'password': data['password'],
                'mafile_path': data['mafile_path'],
                'games': data['games'],
                'api_key': data['api_key'],
                'status': 'free'
            }
            self.save_accounts()
            
            games_str = ", ".join(data['games'])
            update.message.reply_text(f"✅ Аккаунт {login} добавлен с играми: {games_str}")
            
            del user_states[user_id]
            logger.info(f"Добавлен аккаунт: {login}")
            
    def get_free_account(self) -> Optional[str]:
        """Получение свободного аккаунта"""
        for login, data in self.accounts.items():
            if data.get('status') == 'free':
                return login
        return None
        
    def generate_steam_guard_code(self, mafile_path: str) -> Optional[str]:
        """Генерация Steam Guard кода"""
        try:
            with open(mafile_path, 'r') as f:
                mafile_data = json.load(f)
            shared_secret = mafile_data['shared_secret']
            return steam.guard.generate_code(shared_secret)
        except Exception as e:
            logger.error(f"Ошибка генерации кода: {e}")
            return None
            
    def change_password(self, login: str) -> bool:
        """Смена пароля аккаунта (заглушка)"""
        # Здесь должна быть реальная смена пароля через steampy
        logger.info(f"Смена пароля для {login} (заглушка)")
        return True
        
    def send_telegram_notification(self, message: str):
        """Отправка уведомления в Telegram"""
        try:
            self.updater.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
            
    def handle_new_order(self, order):
        """Обработка нового заказа"""
        try:
            chat_id = order.chat_id
            buyer_username = order.buyer.username
            description = order.description
            
            # Получаем свободный аккаунт
            free_login = self.get_free_account()
            if not free_login:
                order.send_message("🚫 Нет свободных аккаунтов.")
                self.send_telegram_notification(f"❌ Нет свободных аккаунтов для заказа {order.id}")
                return
                
            account_data = self.accounts[free_login]
            account_data['status'] = 'rented'
            self.save_accounts()
            
            # Отправляем данные аккаунта
            message = f"""👋 Привет! Я бот. Вот твой аккаунт:
🔑 Логин: {free_login}
🔒 Пароль: {account_data['password']}

📲 Чтобы получить Steam Guard код, напиши !код
🎁 Чтобы получить +30 мин, оставь отзыв после аренды
📞 Для связи с продавцом: !связь
ℹ️ Другие команды: !время, !игры, !помощь"""
            
            order.send_message(message)
            
            # Запускаем аренду
            end_time = time.time() + 3600  # 1 час
            active_rentals[chat_id] = {
                'login': free_login,
                'end_time': end_time,
                'api_key': account_data['api_key'],
                'order_id': order.id,
                'bonus_given': False
            }
            
            # Уведомляем админа
            self.send_telegram_notification(f"🆕 Новый заказ {order.id} от {buyer_username}! Описание: {description}")
            logger.info(f"Новый заказ {order.id}, выдан аккаунт {free_login}")
            
        except Exception as e:
            logger.error(f"Ошибка обработки заказа: {e}")
            
    def handle_new_message(self, message):
        """Обработка нового сообщения в FunPay"""
        try:
            chat_id = message.chat_id
            text = message.text.strip()
            author = message.author
            
            # Игнорируем свои сообщения
            if author == funpay_account.username:
                return
                
            # Проверяем активную аренду
            if chat_id not in active_rentals:
                message.send("🚫 Аккаунт не в аренде.")
                return
                
            rental = active_rentals[chat_id]
            login = rental['login']
            account_data = self.accounts[login]
            
            # Обработка команд
            if text.lower() in ['!код', '!steamguard']:
                code = self.generate_steam_guard_code(account_data['mafile_path'])
                if code:
                    message.send(f"📲 Steam Guard код: {code}")
                    self.send_telegram_notification(f"📲 Запрошен код в чате {chat_id}")
                else:
                    message.send("❌ Ошибка генерации кода.")
                    
            elif text.lower() == '!время':
                remaining = max(0, int(rental['end_time'] - time.time()))
                minutes = remaining // 60
                seconds = remaining % 60
                message.send(f"⏳ Осталось: {minutes} мин {seconds} сек")
                self.send_telegram_notification(f"⏳ Запрошено время в чате {chat_id}")
                
            elif text.lower() == '!игры':
                games = ", ".join(account_data.get('games', []))
                message.send(f"🎮 Игры: {games}")
                self.send_telegram_notification(f"🎮 Запрошен список игр в чате {chat_id}")
                
            elif text.lower() == '!помощь':
                help_text = "ℹ️ Команды: !код — код, !время — время, !игры — игры, !связь — написать продавцу"
                message.send(help_text)
                self.send_telegram_notification(f"ℹ️ Запрошена помощь в чате {chat_id}")
                
            elif text.lower() == '!связь':
                message.send("📩 Напишите ваше сообщение, оно будет отправлено продавцу.")
                pending_contact_messages.add(chat_id)
                self.send_telegram_notification(f"📞 Покупатель в чате {chat_id} хочет связаться")
                
            elif chat_id in pending_contact_messages:
                # Пересылаем сообщение админу
                self.send_telegram_notification(f"📞 Сообщение от покупателя (чат {chat_id}): {text}")
                message.send("📩 Сообщение отправлено продавцу!")
                pending_contact_messages.discard(chat_id)
                
            logger.info(f"Обработано сообщение в чате {chat_id}: {text}")
            
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}")
            
    def rental_monitor(self):
        """Мониторинг аренды"""
        while True:
            try:
                current_time = time.time()
                expired_rentals = []
                
                for chat_id, rental in active_rentals.items():
                    remaining = rental['end_time'] - current_time
                    
                    # Предупреждения
                    if 1800 >= remaining > 1740:  # 30 минут
                        funpay_account.send_message(chat_id, "⚠️ Аренда заканчивается через 30 минут!")
                        logger.info(f"Предупреждение 30 мин для чата {chat_id}")
                    elif 1200 >= remaining > 1140:  # 20 минут
                        funpay_account.send_message(chat_id, "⚠️ Аренда заканчивается через 20 минут!")
                        logger.info(f"Предупреждение 20 мин для чата {chat_id}")
                    elif 600 >= remaining > 540:  # 10 минут
                        funpay_account.send_message(chat_id, "⚠️ Аренда заканчивается через 10 минут!")
                        logger.info(f"Предупреждение 10 мин для чата {chat_id}")
                    
                    # Конец аренды
                    if remaining <= 0:
                        expired_rentals.append(chat_id)
                        
                # Завершаем истекшие аренды
                for chat_id in expired_rentals:
                    rental = active_rentals[chat_id]
                    login = rental['login']
                    
                    # Меняем пароль
                    if self.change_password(login):
                        funpay_account.send_message(chat_id, "🏁 Аренда завершена. Вы кикнуты с аккаунта.")
                        self.send_telegram_notification(f"Аренда для {login} завершена, пароль изменён.")
                    else:
                        self.send_telegram_notification(f"Ошибка смены пароля для {login}")
                        
                    # Освобождаем аккаунт
                    self.accounts[login]['status'] = 'free'
                    self.save_accounts()
                    
                    del active_rentals[chat_id]
                    logger.info(f"Завершена аренда для {login}")
                    
            except Exception as e:
                logger.error(f"Ошибка мониторинга аренды: {e}")
                
            time.sleep(60)  # Проверяем каждую минуту
            
    def bonus_monitor(self):
        """Мониторинг бонусов за отзывы"""
        while True:
            try:
                for chat_id, rental in active_rentals.items():
                    if rental['bonus_given']:
                        continue
                        
                    # Проверяем отзыв (заглушка - нужна реальная проверка через FunPay API)
                    # order = funpay_account.get_order(rental['order_id'])
                    # if order.review_text:
                    #     rental['end_time'] += 1800  # +30 минут
                    #     rental['bonus_given'] = True
                    #     funpay_account.send_message(chat_id, "🎉 Спасибо за отзыв! Добавлено 30 минут аренды!")
                    #     self.send_telegram_notification(f"Бонус добавлен для заказа {rental['order_id']}")
                    #     logger.info(f"Бонус добавлен для заказа {rental['order_id']}")
                    
            except Exception as e:
                logger.error(f"Ошибка мониторинга бонусов: {e}")
                
            time.sleep(300)  # Проверяем каждые 5 минут
            
    def start_funpay_listener(self):
        """Запуск слушателя FunPay"""
        global funpay_account
        
        if not self.funpay_token:
            self.send_telegram_notification("🚀 Бот запущен. Установите FunPay токен: /set_funpay_token")
            return
            
        try:
            funpay_account = Account(self.funpay_token, raise_on_error=True)
            
            # Настраиваем обработчики событий
            funpay_account.add_event_handler(types.EventTypes.NEW_ORDER, self.handle_new_order)
            funpay_account.add_event_handler(types.EventTypes.NEW_MESSAGE, self.handle_new_message)
            
            self.send_telegram_notification("✅ FunPay подключен успешно!")
            logger.info("FunPay слушатель запущен")
            
            # Запускаем слушатель
            funpay_account.listen()
            
        except Exception as e:
            error_msg = f"❌ Ошибка подключения к FunPay: {e}"
            self.send_telegram_notification(error_msg)
            logger.error(error_msg)
            
    def run(self):
        """Запуск бота"""
        logger.info("Запуск Steam Rental Bot")
        
        # Запускаем Telegram бота
        self.updater.start_polling()
        
        # Запускаем мониторы в отдельных потоках
        threading.Thread(target=self.rental_monitor, daemon=True).start()
        threading.Thread(target=self.bonus_monitor, daemon=True).start()
        
        # Запускаем FunPay слушатель
        threading.Thread(target=self.start_funpay_listener, daemon=True).start()
        
        # Запускаем Flask для пинга
        threading.Thread(target=lambda: self.app.run(host='0.0.0.0', port=5000), daemon=True).start()
        
        logger.info("Все сервисы запущены")
        
        # Держим основной поток активным
        self.updater.idle()

if __name__ == "__main__":
    bot = SteamRentalBot()
    bot.run()
