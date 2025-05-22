# DeepStudy Bot

Telegram‑бот, который:

* принимает «темы для изучения» и кладёт их в очередь;
* **раз в сутки** (по заданному пользователем времени GMT+3) генерирует развёрнутую
  статью с помощью DeepSeek Chat и публикует её на [telegra.ph](https://telegra.ph);
* присылает ссылку владельцу темы;
* хранит историю всех разобранных тем;
* может сгенерировать статью **немедленно** по запросу.

## Установка

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # и впишите токены
export $(cat .env | xargs)  # или используйте direnv / systemd
python bot.py
