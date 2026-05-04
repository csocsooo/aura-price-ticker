# AURA DEX Telegram Price Ticker — Setup

Auto-frissülő pinned poszt a `@auradexecosystem` Telegram csatornán.
BTC / ETH / SOL USD ár, óránként frissül, GitHub Actions hostingon (ingyenes, 24/7).

---

## Mit kell megcsinálni (sorrendben)

### 1. Bot létrehozása @BotFather-ben

1. Nyisd meg Telegramon: <https://t.me/BotFather>
2. Küldd: `/newbot`
3. Név (ahogy látszani fog): pl. `AURA Price Bot`
4. Username (kötelezően `bot`-ra végződik, egyedi): pl. `aura_price_ticker_bot`
5. BotFather visszaad egy tokent. Példa formátum:
   `7891234567:AAH...xyz`
6. **Mentsd el ezt a tokent** — ez lesz a `BOT_TOKEN`.

### 2. Bot hozzáadása a csatornához admin jogokkal

1. Menj a `@auradexecosystem` csatornára.
2. Csatorna neve > Manage Channel > Administrators > Add Administrator.
3. Keresd meg a botot a username alapján.
4. **Engedélyek** (kötelező):
   - Post Messages: ON
   - Edit Messages of Others: ON
   - Pin Messages: ON
   - A többi mehet OFF.
5. Save.

### 3. Repó létrehozása GitHub-on

1. Lépj be: <https://github.com>
2. New repository → name: `aura-price-ticker` → **Private** → Create.
3. Töltsd fel ezt a 4 fájlt (drag & drop a GitHub felületén megy):
   - `update_prices.py`
   - `requirements.txt`
   - `.github/workflows/update-prices.yml`
   - `SETUP.md` (opcionális, csak ha referenciának akarod)

### 4. Secret-ek beállítása a repón

A repo oldalán: **Settings → Secrets and variables → Actions → New repository secret**.

Add hozzá ezeket egyenként:

| Név             | Érték                                              |
|-----------------|----------------------------------------------------|
| `BOT_TOKEN`     | A BotFather-től kapott token                       |
| `CHAT_ID`       | `@auradexecosystem`                                |
| `AURA_DEX_URL`  | A pontos AURA DEX URL (pl. `https://auradex.io`)   |
| `MESSAGE_ID`    | **EGYELŐRE HAGYD KI** — a 6. lépésben fogjuk      |

> ⚠️ Az `AURA_DEX_URL`-t ellenőrizni kell — a script default-ja
> `https://auradex.io`, de ha más a hivatalos URL, írd át ezt a secret-et.

### 5. Első futtatás (init mód) — ez küldi és pinneli az üzenetet

1. Repo → **Actions** tab.
2. Bal oldalt: `Update AURA Telegram price ticker` workflow.
3. Jobb oldalt: **Run workflow** → branch `main` → Run workflow.
4. Várj kb. 30 másodpercet, frissítsd az oldalt.
5. Kattints a futásra → kattints a `update` job-ra → nézd meg a logot.
6. A log alján ezt fogod látni:

   ```
   =========================================
     COPY THIS -> MESSAGE_ID = 12345
     Set it as a GitHub Actions secret named MESSAGE_ID.
   =========================================
   ```

7. **Másold ki a számot** (a példában `12345`).

### 6. MESSAGE_ID secret hozzáadása

1. Settings → Secrets and variables → Actions → New repository secret.
2. Név: `MESSAGE_ID`, érték: a kimásolt szám.
3. Save.

### 7. Ellenőrzés

1. Menj vissza a Telegram csatornára. A bot által küldött üzenet már pinned.
2. Actions tabon nyomd meg újra a **Run workflow** gombot.
3. Várj 20 másodpercet. Frissítsd a Telegramot. Az árak frissültek.

Innen óránként magától fut. Kész.

---

## Hibakeresés

**"Telegram editMessageText failed: HTTP 400 chat not found"**
→ Rossz `CHAT_ID`. Csatorna username `@`-cal kezdődjön, vagy adj meg
számot (pl. `-1001234567890`).

**"HTTP 403 not enough rights"**
→ A botnak nincs Edit / Pin joga. Ismételd meg a 2. lépést.

**Nem fut a cron időben**
→ GitHub Actions ingyenes csomagjában a schedule néha 5-15 percet csúszik.
Nem hiba, ez ismert. Ha pontos időzítés kell, a cron sűrűsödhet
(`*/30 * * * *`) vagy fizetős runner kell.

**Üzenet eltűnt vagy átírtad kézzel**
→ Töröld a `MESSAGE_ID` secret-et és futtasd újra. Új üzenetet küld + pin.

**Rate limit / CoinGecko 429**
→ A script 3-szor próbálkozik exponential backoff-fal. Óránként egyszer
fut, gyakorlatilag soha nem üt rate limitbe.

---

## Költség

GitHub Actions Free: 2000 perc/hó privát repón.
Ez a workflow ~20 másodperc futás × 24 × 30 = **~4 perc/hó**.
Nem kerül semmibe.

---

## Mit tudsz még csinálni

- **Sűrűbb frissítés:** workflow YAML-ben `cron: "0 * * * *"` →
  `cron: "*/15 * * * *"` (15 percenként). 5 percnél sűrűbbet ne tegyél,
  GitHub Actions úgyis behúzza.
- **24h változás % vagy több token:** szólj és bővítem.
- **Több csatorna:** matrix strategy-vel ugyanez a workflow több
  csatornára is mehet.
