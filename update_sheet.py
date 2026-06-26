import os
import json
import sys
import io
import zipfile
import requests
import gspread
import pandas as pd
import numpy as np

from datetime import datetime, timedelta
from oauth2client.service_account import ServiceAccountCredentials

CONFIG = {
    "SPREADSHEET_KEY": "1zzEuAn8rXujdqCYTdHrZW67Bg-7vLh_Ho58hzg969_E",
    "EMA_LEN": 21,
    "RSI_LEN": 10,
    "HISTORICAL_DAYS": 300,
}

print("🚀 Swing Institutional Scanner Started...")

# =========================================================
# AUTO DATE
# =========================================================

def get_latest_trading_date():
    today = datetime.now()

    for i in range(10):
        test_date = today - timedelta(days=i)

        if test_date.weekday() < 5:
            return test_date

    return today


target_date = get_latest_trading_date()
fetched_date_str = target_date.strftime("%d-%b-%Y")

print(f"📅 Using Date: {fetched_date_str}")
#target_date = datetime(2026, 6, 3)
#fetched_date_str = target_date.strftime("%d-%b-%Y")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}

# =========================================================
# GOOGLE SHEET SETUP
# =========================================================

print("🔐 STARTING GOOGLE AUTH")

creds_json = os.environ.get("GCP_CREDENTIALS")

if not creds_json:
    raise Exception("❌ GCP_CREDENTIALS environment variable not found!")

try:
    creds_dict = json.loads(creds_json)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict,
        scope
    )

    client = gspread.authorize(creds)

    spreadsheet = client.open_by_key(CONFIG["SPREADSHEET_KEY"])

    try:
        worksheet_top = spreadsheet.worksheet("TOP 250")
    except:
        worksheet_top = spreadsheet.add_worksheet(
            title="TOP 250",
            rows=1000,
            cols=100
        )

    try:
        worksheet_final = spreadsheet.worksheet("FINAL LIST")
    except:
        worksheet_final = spreadsheet.add_worksheet(
            title="FINAL LIST",
            rows=1000,
            cols=100
        )

    print("✅ GOOGLE SHEET CONNECTED")

except Exception as e:
    print("❌ Google Sheet Error:", repr(e))
    raise


# =========================================================
# HELPERS
# =========================================================

def get_previous_trading_day(date_obj, days_back=0):
    date = date_obj
    found = 0

    while True:
        if date.weekday() < 5:
            if found == days_back:
                return date
            found += 1

        date -= timedelta(days=1)


def calc_ema(series, length):
    return series.ewm(
        span=length,
        adjust=False,
        min_periods=length
    ).mean()


def calc_rsi(series, length):
    delta = series.diff()

    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi.replace([np.inf, -np.inf], np.nan)


# =========================================================
# HISTORICAL CASH DATA
# =========================================================

def fetch_historical_prices(target_date, days=CONFIG["HISTORICAL_DAYS"]):

    print(f"📥 Fetching Last {days} Trading Days Cash Data...")

    all_data = []
    count = 0
    i = 0

    while count < days and i < 350:

        curr_date = get_previous_trading_day(target_date, i)

        try:
            date_str = curr_date.strftime("%Y%m%d")

            url = (
                "https://nsearchives.nseindia.com/content/cm/"
                f"BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"
            )

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            if response.status_code != 200:
                i += 1
                continue

            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    df = pd.read_csv(f)

            sym_col = next((c for c in ["TckrSymb", "SYMBOL"] if c in df.columns), None)
            open_col = next((c for c in ["OpnPric", "OPEN"] if c in df.columns), None)
            high_col = next((c for c in ["HghPric", "HIGH"] if c in df.columns), None)
            low_col = next((c for c in ["LwPric", "LOW"] if c in df.columns), None)
            close_col = next((c for c in ["ClsPric", "CLOSE"] if c in df.columns), None)
            vol_col = next((c for c in ["TtlTradgVol", "TOTTRDQTY"] if c in df.columns), None)
            series_col = next((c for c in ["SctySrs", "SERIES"] if c in df.columns), None)

            if not all([sym_col, open_col, high_col, low_col, close_col, vol_col, series_col]):
                i += 1
                continue

            df = df[
                df[series_col].astype(str).str.strip() == "EQ"
            ].copy()

            df = df[
                [sym_col, open_col, high_col, low_col, close_col, vol_col]
            ].copy()

            df.columns = [
                "SYMBOL",
                "OPEN",
                "HIGH",
                "LOW",
                "CLOSE",
                "CASH_VOLUME"
            ]

            df["DATE"] = curr_date.strftime("%Y-%m-%d")
            df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()

            for col in ["OPEN", "HIGH", "LOW", "CLOSE", "CASH_VOLUME"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            all_data.append(df)
            count += 1

            print(f"   ✅ {curr_date.strftime('%d-%b')} loaded")

        except Exception as e:
            print(f"   ❌ Cash Error {curr_date.strftime('%d-%b')}: {e}")

        i += 1

    if not all_data:
        return None

    combined = pd.concat(all_data, ignore_index=True)

    print(f"✅ Historical Data Loaded: {len(combined)} records")

    return combined


# =========================================================
# FUTURES DATA
# =========================================================

def fetch_fo_data(target_date):

    print("\n📥 Searching Latest Futures Data...")

    for attempt in range(20):

        curr_date = get_previous_trading_day(target_date, attempt)

        try:
            date_str = curr_date.strftime("%Y%m%d")

            url = (
                "https://nsearchives.nseindia.com/content/fo/"
                f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"
            )

            print(f"   Trying: {curr_date.strftime('%d-%b-%Y')}")

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            if response.status_code != 200:
                continue

            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    df = pd.read_csv(f)

            inst_col = next((c for c in ["FinInstrmTp", "INSTRUMENT"] if c in df.columns), None)
            sym_col = next((c for c in ["TckrSymb", "SYMBOL"] if c in df.columns), None)
            expiry_col = next((c for c in ["XpryDt", "EXPIRY_DT"] if c in df.columns), None)
            oi_col = next((c for c in ["OpnIntrst", "OPEN_INT", "OPEN_INTEREST"] if c in df.columns), None)
            vol_col = next((c for c in ["TtlTradgVol", "CONTRACTS"] if c in df.columns), None)
            close_col = next((c for c in ["ClsPric", "CLOSE", "SETTLE_PR"] if c in df.columns), None)

            if not all([inst_col, sym_col, expiry_col, oi_col, vol_col, close_col]):
                continue

            df[inst_col] = df[inst_col].astype(str).str.upper().str.strip()

            df = df[
                df[inst_col].isin([
                    "FUTSTK",
                    "FUTIDX",
                    "STF",
                    "IDF",
                    "STOCK FUTURES",
                    "INDEX FUTURES",
                    "FUTSTOCK",
                    "FUTINDEX"
                ])
            ].copy()

            if df.empty:
                continue

            df[expiry_col] = pd.to_datetime(df[expiry_col], errors="coerce")
            nearest_expiry = df[expiry_col].min()

            df = df[df[expiry_col] == nearest_expiry].copy()

            for col in [oi_col, vol_col, close_col]:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

            fo_df = df[[sym_col, vol_col, oi_col, close_col]].copy()

            fo_df.columns = [
                "SYMBOL",
                "FUT_VOLUME",
                "OPEN_INTEREST",
                "FUT_CLOSE"
            ]

            fo_df["SYMBOL"] = fo_df["SYMBOL"].astype(str).str.strip()

            print(
                f"✅ FO Data Loaded: {len(fo_df)} stocks "
                f"({curr_date.strftime('%d-%b-%Y')})"
            )

            return fo_df, curr_date

        except Exception as e:
            print(f"   ❌ FO Error: {e}")

    return None, None


# =========================================================
# PREVIOUS DAY FUTURES OI DATA
# =========================================================

def fetch_previous_fo_oi(actual_date):

    print("\n📥 Fetching Previous Day FO OI...")

    for attempt in range(1, 20):

        curr_date = get_previous_trading_day(actual_date, attempt)

        try:
            date_str = curr_date.strftime("%Y%m%d")

            url = (
                "https://nsearchives.nseindia.com/content/fo/"
                f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"
            )

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            if response.status_code != 200:
                continue

            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    df = pd.read_csv(f)

            inst_col = next((c for c in ["FinInstrmTp", "INSTRUMENT"] if c in df.columns), None)
            sym_col = next((c for c in ["TckrSymb", "SYMBOL"] if c in df.columns), None)
            expiry_col = next((c for c in ["XpryDt", "EXPIRY_DT"] if c in df.columns), None)
            oi_col = next((c for c in ["OpnIntrst", "OPEN_INT", "OPEN_INTEREST"] if c in df.columns), None)

            if not all([inst_col, sym_col, expiry_col, oi_col]):
                continue

            df[inst_col] = df[inst_col].astype(str).str.upper().str.strip()

            df = df[
                df[inst_col].isin([
                    "FUTSTK",
                    "FUTIDX",
                    "STF",
                    "IDF",
                    "STOCK FUTURES",
                    "INDEX FUTURES",
                    "FUTSTOCK",
                    "FUTINDEX"
                ])
            ].copy()

            if df.empty:
                continue

            df[expiry_col] = pd.to_datetime(df[expiry_col], errors="coerce")
            nearest_expiry = df[expiry_col].min()

            df = df[df[expiry_col] == nearest_expiry].copy()

            df[oi_col] = pd.to_numeric(df[oi_col], errors="coerce").fillna(0)

            prev_oi_df = df[[sym_col, oi_col]].copy()

            prev_oi_df.columns = [
                "SYMBOL",
                "PREV_OPEN_INTEREST"
            ]

            prev_oi_df["SYMBOL"] = prev_oi_df["SYMBOL"].astype(str).str.strip()

            print(
                f"✅ Previous FO OI Loaded: "
                f"{curr_date.strftime('%d-%b-%Y')}"
            )

            return prev_oi_df

        except Exception as e:
            print(f"   ❌ Prev FO Error: {e}")

    print("❌ Previous FO OI not found")

    return None


# =========================================================
# DELIVERY DATA
# =========================================================

def fetch_delivery_data(actual_date):

    print("\n📥 Fetching Delivery Data...")

    try:
        date_str = actual_date.strftime("%d%m%Y")

        url = (
            "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_"
            f"{date_str}.csv"
        )

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )

        if response.status_code != 200:
            print("❌ Delivery file not found")
            return None

        df = pd.read_csv(io.StringIO(response.text))

        df.columns = [c.strip() for c in df.columns]

        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()

        df["DELIV_QTY"] = pd.to_numeric(
            df["DELIV_QTY"],
            errors="coerce"
        ).fillna(0)

        df["TTL_TRD_QNTY"] = pd.to_numeric(
            df["TTL_TRD_QNTY"],
            errors="coerce"
        ).fillna(0)

        df["DELIVERY_%"] = np.where(
            df["TTL_TRD_QNTY"] > 0,
            (df["DELIV_QTY"] / df["TTL_TRD_QNTY"]) * 100,
            0
        )

        delivery_df = df[[
            "SYMBOL",
            "DELIVERY_%"
        ]].copy()

        delivery_df["DELIVERY_%"] = delivery_df["DELIVERY_%"].round(2)

        print("✅ Delivery Data Loaded")

        return delivery_df

    except Exception as e:
        print(f"❌ Delivery Error: {e}")
        return None
HTF_PRD = 10
ZONE_PER = 0.2

def last_pivot_high(series, left=10, right=10):
    series = series.dropna().reset_index(drop=True)

    if len(series) < left + right + 1:
        return np.nan

    ph = np.nan

    for i in range(left, len(series) - right):
        window = series.iloc[i-left:i+right+1]

        if series.iloc[i] == window.max():
            ph = float(series.iloc[i])

    return ph


def last_pivot_low(series, left=10, right=10):
    series = series.dropna().reset_index(drop=True)

    if len(series) < left + right + 1:
        return np.nan

    pl = np.nan

    for i in range(left, len(series) - right):
        window = series.iloc[i-left:i+right+1]

        if series.iloc[i] == window.min():
            pl = float(series.iloc[i])

    return pl


def get_htf_zone(df, rule):
    temp = df.copy()

    temp["DATE"] = pd.to_datetime(temp["DATE"], errors="coerce")
    temp = temp.dropna(subset=["DATE"])

    if temp.empty:
        return np.nan, np.nan, np.nan, np.nan

    htf = (
        temp.set_index("DATE")
        .resample(rule)
        .agg({
            "HIGH": "max",
            "LOW": "min",
            "CLOSE": "last"
        })
        .dropna()
        .reset_index()
    )

    if len(htf) < HTF_PRD * 2 + 1:
        return np.nan, np.nan, np.nan, np.nan

    ph = last_pivot_high(htf["HIGH"], HTF_PRD, HTF_PRD)
    pl = last_pivot_low(htf["LOW"], HTF_PRD, HTF_PRD)

    supply_top = ph
    supply_bot = ph - (ph * ZONE_PER / 100) if not np.isnan(ph) else np.nan

    demand_bot = pl
    demand_top = pl + (pl * ZONE_PER / 100) if not np.isnan(pl) else np.nan

    return supply_top, supply_bot, demand_top, demand_bot

# =========================================================
# MAIN EXECUTION
# =========================================================

hist_prices = fetch_historical_prices(target_date)
fo_df, actual_date = fetch_fo_data(target_date)

if hist_prices is None or hist_prices.empty:
    raise Exception("❌ Historical cash data not available.")

if fo_df is None or fo_df.empty:
    raise Exception("❌ Futures data not available.")

print("\n⚙️ Calculating EMA RSI + Analysis...")

hist_prices["DATE"] = pd.to_datetime(hist_prices["DATE"], errors="coerce")
hist_prices = hist_prices.dropna(subset=["DATE"])

hist_calc_list = []

for symbol, data in hist_prices.groupby("SYMBOL"):

    data = data.sort_values("DATE").copy()

    data["EMA_AVG"] = calc_ema(
        data["CLOSE"],
        CONFIG["EMA_LEN"]
    )

    data["EMA_RSI"] = calc_rsi(
        data["EMA_AVG"],
        CONFIG["RSI_LEN"]
    )

    data["AVG_20_VOLUME"] = (
        data["CASH_VOLUME"]
        .rolling(20)
        .mean()
    )

    data["VOL_SPIKE"] = (
        data["CASH_VOLUME"]
        / data["AVG_20_VOLUME"]
    )

    data["EMA_100"] = calc_ema(
        data["CLOSE"],
        100
    )

    data["TREND_STATUS"] = np.where(
        data["CLOSE"] > data["EMA_100"],
        "BULLISH",
        "BEARISH"
    )

    # =====================================================
    # HTF DEMAND SUPPLY
    # =====================================================

    d_sup_top, d_sup_bot, d_dem_top, d_dem_bot = get_htf_zone(data, "D")
    w_sup_top, w_sup_bot, w_dem_top, w_dem_bot = get_htf_zone(data, "W")
    m_sup_top, m_sup_bot, m_dem_top, m_dem_bot = get_htf_zone(data, "ME")

    last_close = data["CLOSE"].iloc[-1]

    ZONE_BUFFER = 3

    demand_hit = (
        (
            not np.isnan(d_dem_bot)
            and last_close <= d_dem_top * (1 + ZONE_BUFFER / 100)
            and last_close >= d_dem_bot * (1 - ZONE_BUFFER / 100)
        )
        or
        (
            not np.isnan(w_dem_bot)
            and last_close <= w_dem_top * (1 + ZONE_BUFFER / 100)
            and last_close >= w_dem_bot * (1 - ZONE_BUFFER / 100)
        )
        or
        (
            not np.isnan(m_dem_bot)
            and last_close <= m_dem_top * (1 + ZONE_BUFFER / 100)
            and last_close >= m_dem_bot * (1 - ZONE_BUFFER / 100)
        )
    )

    supply_hit = (
        (
            not np.isnan(d_sup_bot)
            and last_close <= d_sup_top * (1 + ZONE_BUFFER / 100)
            and last_close >= d_sup_bot * (1 - ZONE_BUFFER / 100)
        )
        or
        (
            not np.isnan(w_sup_bot)
            and last_close <= w_sup_top * (1 + ZONE_BUFFER / 100)
            and last_close >= w_sup_bot * (1 - ZONE_BUFFER / 100)
        )
        or
        (
            not np.isnan(m_sup_bot)
            and last_close <= m_sup_top * (1 + ZONE_BUFFER / 100)
            and last_close >= m_sup_bot * (1 - ZONE_BUFFER / 100)
        )
    )

    data["NEAR_DEMAND_ZONE"] = (
        "YES" if demand_hit else "NO"
    )

    data["NEAR_SUPPLY_ZONE"] = (
        "YES" if supply_hit else "NO"
    )

    # =====================================================
    # PRICE CHANGE
    # =====================================================

    data["PRICE_CHANGE_%"] = (
        data["CLOSE"].pct_change() * 100
    )

    data["SYMBOL"] = symbol

    hist_calc_list.append(data)

hist_calc = pd.concat(hist_calc_list, ignore_index=True)

latest_date = hist_calc["DATE"].max()

latest_indicators = hist_calc[
    hist_calc["DATE"] == latest_date
][[
    "SYMBOL",
    "CLOSE",
    "EMA_RSI",
    "VOL_SPIKE",
    "TREND_STATUS",
    "NEAR_DEMAND_ZONE",
    "NEAR_SUPPLY_ZONE",
    "PRICE_CHANGE_%"
]].copy()

final_df = pd.merge(
    fo_df,
    latest_indicators,
    on="SYMBOL",
    how="left"
)

final_df["CLOSE"] = final_df["CLOSE"].fillna(final_df["FUT_CLOSE"])

for col in [
    "EMA_RSI",
    "VOL_SPIKE",
    "PRICE_CHANGE_%"
]:
    final_df[col] = pd.to_numeric(
        final_df[col],
        errors="coerce"
    ).fillna(0)

# =========================================================
# MERGE DELIVERY
# =========================================================

delivery_df = fetch_delivery_data(actual_date)

if delivery_df is not None:
    final_df = pd.merge(
        final_df,
        delivery_df,
        on="SYMBOL",
        how="left"
    )
else:
    final_df["DELIVERY_%"] = 0

final_df["DELIVERY_%"] = pd.to_numeric(
    final_df["DELIVERY_%"],
    errors="coerce"
).fillna(0).round(2)

# =========================================================
# REAL OI CHANGE %
# =========================================================

prev_oi_df = fetch_previous_fo_oi(actual_date)

if prev_oi_df is not None:
    final_df = pd.merge(
        final_df,
        prev_oi_df,
        on="SYMBOL",
        how="left"
    )
else:
    final_df["PREV_OPEN_INTEREST"] = 0

final_df["PREV_OPEN_INTEREST"] = pd.to_numeric(
    final_df["PREV_OPEN_INTEREST"],
    errors="coerce"
).fillna(0)

final_df["OI_CHANGE_%"] = np.where(
    final_df["PREV_OPEN_INTEREST"] > 0,
    (
        (final_df["OPEN_INTEREST"] - final_df["PREV_OPEN_INTEREST"])
        / final_df["PREV_OPEN_INTEREST"]
    ) * 100,
    0
)

final_df["OI_CHANGE_%"] = final_df["OI_CHANGE_%"].round(2)

# =========================================================
# OB / OS STATUS
# =========================================================

final_df["OB_OS_STATUS"] = np.where(
    final_df["EMA_RSI"] >= 90,
    "BUY",
    np.where(
        final_df["EMA_RSI"] <= 10,
        "SELL",
        "NORMAL"
    )
)

# =========================================================
# SWING LOGIC
# =========================================================

final_df["SWING_SIGNAL"] = np.where(
    (
        (final_df["OB_OS_STATUS"] == "SELL") &
        (final_df["TREND_STATUS"] == "BULLISH") &
        (final_df["NEAR_DEMAND_ZONE"] == "YES") &
        (final_df["VOL_SPIKE"] >= 1.2) &
        (final_df["DELIVERY_%"] >= 35) &
        (final_df["OI_CHANGE_%"] <= 10)
    ),
    "HIGH PROBABILITY BUY",
    np.where(
        (
            (final_df["OB_OS_STATUS"] == "BUY") &
            (final_df["TREND_STATUS"] == "BEARISH") &
            (final_df["NEAR_SUPPLY_ZONE"] == "YES") &
            (final_df["VOL_SPIKE"] >= 1.2) &
            (final_df["OI_CHANGE_%"] >= 10)
        ),
        "HIGH PROBABILITY SELL",
        "WATCH"
    )
)

# =========================================================
# CONFIDENCE SCORE
# =========================================================

final_df["CONFIDENCE_SCORE"] = 0

final_df.loc[
    final_df["OB_OS_STATUS"].isin(["BUY", "SELL"]),
    "CONFIDENCE_SCORE"
] += 25

final_df.loc[
    final_df["VOL_SPIKE"] >= 1.2,
    "CONFIDENCE_SCORE"
] += 15

final_df.loc[
    final_df["DELIVERY_%"] >= 35,
    "CONFIDENCE_SCORE"
] += 15

final_df.loc[
    final_df["OI_CHANGE_%"] <= 10,
    "CONFIDENCE_SCORE"
] += 15

final_df.loc[
    final_df["NEAR_DEMAND_ZONE"] == "YES",
    "CONFIDENCE_SCORE"
] += 10

final_df.loc[
    final_df["NEAR_SUPPLY_ZONE"] == "YES",
    "CONFIDENCE_SCORE"
] += 10

final_df.loc[
    final_df["SWING_SIGNAL"].isin([
        "HIGH PROBABILITY BUY",
        "HIGH PROBABILITY SELL"
    ]),
    "CONFIDENCE_SCORE"
] += 25

# =========================================================
# ROUNDING
# =========================================================

final_df["EMA_RSI"] = final_df["EMA_RSI"].round(2)
final_df["VOL_SPIKE"] = final_df["VOL_SPIKE"].round(2)
final_df["PRICE_CHANGE_%"] = final_df["PRICE_CHANGE_%"].round(2)

final_df = final_df.fillna(0)

# ===============================
# FINAL LIST
# ===============================

buy_list = final_df[
    final_df["EMA_RSI"] >= 90
].copy()

buy_list = buy_list.sort_values(
    by="EMA_RSI",
    ascending=True
)

sell_list = final_df[
    final_df["EMA_RSI"] <= 10
].copy()

sell_list = sell_list.sort_values(
    by="EMA_RSI",
    ascending=False
)

final_list = pd.concat(
    [buy_list, sell_list],
    ignore_index=True
)

final_list.insert(
    0,
    "SR_NO",
    range(1, len(final_list) + 1)
)

# =========================================================
# TRADINGVIEW CLICKABLE LINK
# =========================================================

for df in [final_df, final_list]:

    df["TV_LINK"] = df["SYMBOL"].apply(
        lambda x: f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{x}","{x}")'
    )

    df.drop(
        columns=["SYMBOL"],
        inplace=True,
        errors="ignore"
    )

    df.rename(
        columns={"TV_LINK": "SYMBOL"},
        inplace=True
    )

# =========================================================
# REMOVE UNWANTED COLUMNS FROM TOP 250 AND FINAL LIST
# =========================================================

remove_cols = [
    "FUT_VOLUME",
    "FUT_CLOSE",
    "CLOSE",
    "OPEN_INTEREST",
    "VOL_SPIKE",
    "TREND_STATUS",
    "PRICE_CHANGE_%",
    "PREV_OPEN_INTEREST",
    "SWING_SIGNAL",
    "CONFIDENCE_SCORE"
]

for df in [final_df, final_list]:

    df.drop(
        columns=remove_cols,
        inplace=True,
        errors="ignore"
    )


# =========================================================
# ADD DATE COLUMN IN COLUMN I SAFELY
# =========================================================

date_value = actual_date.strftime("%d-%b-%Y")


def add_date_column_i(df):
    if "DATE" in df.columns:
        df.drop(columns=["DATE"], inplace=True)

    while len(df.columns) < 8:
        blank_col = f"BLANK_{len(df.columns) + 1}"

        while blank_col in df.columns:
            blank_col = blank_col + "_"

        df[blank_col] = ""

    df.insert(
        8,
        "DATE",
        date_value
    )

    return df


final_df = add_date_column_i(final_df)
final_list = add_date_column_i(final_list)

# =========================================================
# UPDATE GOOGLE SHEETS
# DATA ANALYSIS SHEET REMOVED
# =========================================================

print("\n📤 Updating Google Sheets...")

worksheet_top.clear()
worksheet_top.resize(rows=1000, cols=100)

top_data = [
    final_df.columns.tolist()
] + final_df.values.tolist()

worksheet_top.update(
    "A1",
    top_data,
    value_input_option="USER_ENTERED"
)

worksheet_final.clear()
worksheet_final.resize(rows=1000, cols=100)

final_data = [
    final_list.columns.tolist()
] + final_list.values.tolist()

worksheet_final.update(
    "A1",
    final_data,
    value_input_option="USER_ENTERED"
)

ist_now = (
    datetime.utcnow() +
    timedelta(hours=5, minutes=30)
).strftime("%d-%b %H:%M")

status = (
    f"EMA RSI OB/OS | "
    f"Data: {actual_date.strftime('%d-%b-%Y')} | "
    f"Updated: {ist_now} IST"
)

worksheet_top.update("X1", [[status]])
worksheet_final.update("P1", [[status]])

print(f"\n🎉 SUCCESS! {len(final_df)} Future Stocks Updated")
print(f"✅ FINAL LIST: {len(final_list)} OB/OS Stocks")
print("✅ DATA ANALYSIS Sheet Update Removed")
print(f"🕒 Last Updated: {ist_now} IST")
