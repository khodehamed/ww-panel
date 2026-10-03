# ww-panel

پنل کنترل مستقل برای تانل‌های **WaterWall Proto51**. فقط وضعیت را نشان می‌دهد، پارامترها را ویرایش می‌کند و در صورت نیاز تانل ایزولهٔ جدید می‌سازد.

این پروژه **جدا از خود تانل‌ها** است. نصب پنل، سرویس‌های زیر را متوقف یا ویرایش **نمی‌کند**:

- `waterwall-proto51` / `ww51` / `wtun0`
- `waterwall-proto51-v2` / `ww51v2` / `wtun2`
- هر اینترفیس یا یونیت **backhaul** (فقط نمایش، قفل‌شده)

مخزن: <https://github.com/khodehamed/ww-panel>

## نصب

روی سرور لینوکس (Debian/Ubuntu)، به‌عنوان root:

```bash
curl -fsSL https://raw.githubusercontent.com/khodehamed/ww-panel/master/install.sh | sudo bash
```

یا بعد از کلون:

```bash
sudo bash install.sh
```

منوی نصب شبیه `ww51` است:

1. Install / Reinstall  
2. Status  
3. Restart panel  
4. Change panel port  
5. Set password  
6. Uninstall  
0. Exit  

دستورهای بعدی:

```bash
sudo wwpanel
# یا
sudo ww-panel
```

### پورت و فایروال

- پورت پیش‌فرض: **12010**
- بایند: **0.0.0.0** (از همه اینترفیس‌ها)
- مقدارها در `/opt/ww-panel/panel.env` ذخیره می‌شوند
- پورت را از منوی `4` عوض کنید، سپس TCP همان پورت را روی فایروال/security group باز کنید

```bash
# مثال ufw (فقط اگر خودتان ufw را مدیریت می‌کنید)
sudo ufw allow 12010/tcp
```

### ورود پیش‌فرض

- نام کاربری: `admin`
- رمز عبور: هنگام اولین نصب **یک‌بار** ساخته و چاپ می‌شود. ذخیره کنید؛ فقط هش در `panel.env` می‌ماند.
- تغییر رمز: `sudo wwpanel` → گزینهٔ 5

پنل روی `http://<IP-سرور>:12010/` باز می‌شود.

## چه چیزی کشف می‌شود؟

پنل این منابع را اسکن می‌کند:

1. پوشه‌های `/opt/waterwall-proto51*` و فایل `tunnel.env`
2. یونیت‌های systemd با الگوی `waterwall-proto51*`
3. اینترفیس‌های `wtun*` (مثل `wtun0` = `10.10.0.0/24` و `wtun2` = `10.10.1.0/24`)
4. بک‌هال: اینترفیس به نام `backhaul` یا یونیت‌های `backhaul-*` — **فقط نمایش**
5. کانفیگ JSON واتروال بدون `tunnel.env` — **فقط‌خواندنی**

برای هر تانل: وضعیت systemd، بالا بودن tun و IP، پینگ پیر تونل (`ping -c1 -W1` روی `10.10.x.2` / `.1`)، PROTO، ENCRYPT، تأخیر پینگ، و پهنای‌باند از `/proc/net/dev` (نمودار حدود ۵ دقیقه).

کلیدهای قابل ویرایش (اگر `tunnel.env` باشد و بک‌هال نباشد):

`IRAN_IP` `KHAREJ_IP` `PROTO` `PORTS` `ENCRYPT` `AES_KEY` `PORT_OFFSET` `ENC_ALGO`

بعد از ذخیره، `tunnel.env` بکاپ می‌شود و مسیر apply موجود صدا زده می‌شود:

- `ww51 apply`
- `ww51v2 apply`
- یا `bash /opt/.../install.sh apply`

فقط **همان یونیت** ریستارت می‌شود.

## تانل جدید

جادوگر، پروفایل ایزوله `/opt/waterwall-proto51-tN` با tun/subnet آزاد می‌سازد (مثلاً `wtun3` / `10.10.3.0/24`). باینری WaterWall از v2 یا v1 کپی می‌شود؛ در غیر این صورت از ریلیز رسمی دانلود می‌گردد. v1 و v2 دست نخورده می‌مانند.

## ایمنی

- روی سرورهای زنده از این مخزن نصب کنید؛ این README شما را به SSH به سرورهای دیگر تشویق نمی‌کند.
- بک‌هال همیشه قفل است: API اجازهٔ edit/start/stop/restart نمی‌دهد.
- پنل با root اجرا می‌شود تا بتواند `systemctl` و `/opt` را مدیریت کند. آن را پشت فایروال و رمز قوی بگذارید.

## توسعه محلی

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
WWPANEL_ENV=./panel.env .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 12010
```

## مجوز

MIT — فایل `LICENSE`.
