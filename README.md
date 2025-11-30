

# Terminal Docker Bot <div align="center">

<img src="img/main_img.png" alt="Bot Preview" width="300"/>

</div>

[Telegram bot](https://t.me/docker_terminal_bot)  [Telegram](https://t.me/Hairpin00)
 
## 🚀 Установка
```bash
# Клонирование репозитория
git clone https://github.com/hairpin00/terminal-docker-bot.git
cd terminal-docker-bot
```
```bash
# Установка зависимостей
pip install -r requirements.txt
```
```bash
# Настройка переменных окружения
export TELEGRAM_BOT_TOKEN="your_bot_token_here" # можете добвавить это в кфг вашего шела (.zshrc, .bashrc)
```
```bash
# Запуск Redis (Ubuntu/Debian)
sudo apt update && sudo apt install redis-server
sudo systemctl start redis
```
```bash
# Запуск бота
python3 terminal-docker-bot.py
```
> [!TIP]
> ⚠️ Примечание
```bash
# Для корректной работы необходимы:
- Docker Engine (+ права пользователя)
- Redis Server на localhost:6379
- Python 3.8+ с пакетами из requirements.txt
- Telegram Bot Token от @BotFather

# Проверка установки
docker --version        # Должна быть установлена Docker
redis-cli ping          # Должен ответить PONG
python --version        # Должна быть версия 3.8+
```
