import pandas as pd

def calculate_sma(history, period=20):
 return history["Close"].rolling(window=period).mean()
def calculate_ema(history, period=20):
    return history["Close"].ewm(span=period, adjust=False).mean()   
def calculate_rsi(history, period=14):
    delta = history["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi
def calculate_macd(history, fast=12, slow=26, signal=9):
    fast_ema = history["Close"].ewm(span=fast, adjust=False).mean()
    slow_ema = history["Close"].ewm(span=slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal_line = macd.ewm(span=signal, adjust=False).mean() 
    histogram = macd - signal_line
    return macd, signal_line, histogram
def calculate_bollinger_bands(history, period=20, std_dev=2):
    middle_band = history["Close"].rolling(window=period).mean()
    rolling_std = history["Close"].rolling(window=period).std()
    upper_band = middle_band + (rolling_std * std_dev)
    lower_band = middle_band - (rolling_std * std_dev)
    return middle_band, upper_band, lower_band
def calculate_atr(history, period=14):
    high_low = history["High"] - history["Low"]
    high_close = (history["High"] - history["Close"].shift()).abs()
    low_close = (history["Low"] - history["Close"].shift()).abs()
    true_range = high_low.combine(high_close, max).combine(low_close, max)
    atr = true_range.rolling(window=period).mean()
    return atr
def calculate_vwap(history):
    typical_price = (history["High"] + history["Low"] + history["Close"]) / 3
    price_volume = typical_price * history["Volume"]
    vwap = price_volume.cumsum() / history["Volume"].cumsum()
    return vwap
def calculate_stochastic(history, period=14):
    lowest_low = history["Low"].rolling(window=period).min()
    highest_high = history["High"].rolling(window=period).max()
    k = 100 * (history["Close"] - lowest_low) / (highest_high - lowest_low)
    d = k.rolling(window=3).mean()
    return k, d
def calculate_obv(history):
    obv = history["Volume"].copy()
    obv.iloc[0] = history["Volume"].iloc[0]
    for i in range(1, len(history)):
      if history["Close"].iloc[i] > history["Close"].iloc[i - 1]:
        obv.iloc[i] = obv.iloc[i - 1] + history["Volume"].iloc[i]
      else:
       obv.iloc[i] = obv.iloc[i - 1] - history["Volume"].iloc[i]
       return obv  
def calculate_adx(history, period=14):
    plus_dm = history["High"].diff()
    minus_dm = history["Low"].shift() - history["Low"]
    plus_dm[(plus_dm <= minus_dm) | (plus_dm < 0)] = 0
    minus_dm[(minus_dm <= plus_dm) | (minus_dm < 0)] = 0
    high_low = history["High"] - history["Low"]
    high_close = (history["High"] - history["Close"].shift()).abs()
    low_close = (history["Low"] - history["Close"].shift()).abs()
    true_range = high_low.combine(high_close, max).combine(low_close, max)
    
    atr = true_range.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()
    return adx
def calculate_cci(history, period=20):
    typical_price = (history["High"] + history["Low"] + history["Close"]) / 3
    sma = typical_price.rolling(window=period).mean()
    mean_deviation = typical_price.rolling(window=period).apply(lambda x: abs(x - x.mean()).mean())
    cci = (typical_price - sma) / (0.015 * mean_deviation)
    return cci
def calculate_williams_r(history, period=14):
    highest_high = history["High"].rolling(window=period).max()
    lowest_low = history["Low"].rolling(window=period).min()
    williams_r = -100 * (highest_high - history["Close"]) / (highest_high - lowest_low)
    return williams_r
def calculate_roc(history, period=12):
    roc = ((history["Close"] - history["Close"].shift(period)) / history["Close"].shift(period)) * 100
    return roc
def calculate_mfi(history, period=14):
    typical_price = (history["High"] + history["Low"] + history["Close"]) / 3
    raw_money_flow = typical_price * history["Volume"]
    positive_flow = raw_money_flow.where(typical_price > typical_price.shift(), 0)
    negative_flow = raw_money_flow.where(typical_price < typical_price.shift(), 0)
    positive_sum = positive_flow.rolling(window=period).sum()
    negative_sum = negative_flow.rolling(window=period).sum()
    money_ratio = positive_sum / negative_sum
    mfi = 100 - (100 / (1 + money_ratio))
    return mfi
def calculate_momentum(history, period=10):
    momentum = history["Close"] - history["Close"].shift(period)
    return momentum

def calculate_trix(history, period=15):
    ema1 = history["Close"].ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    trix = ema3.pct_change() * 100
    return trix
def calculate_ultimate_oscillator(history, short=7, medium=14, long=28):
    previous_close = history["Close"].shift(1)
    buying_pressure = history["Close"] - history["Low"].combine(previous_close, min)
    true_range = history["High"].combine(previous_close, max) - history["Low"].combine(previous_close, min)
    avg_short = buying_pressure.rolling(short).sum() / true_range.rolling(short).sum()
    avg_medium = buying_pressure.rolling(medium).sum() / true_range.rolling(medium).sum()
    avg_long = buying_pressure.rolling(long).sum() / true_range.rolling(long).sum()
    ultimate = 100 * ((4 * avg_short) + (2 * avg_medium) + avg_long) / 7
    return ultimate
def calculate_awesome_oscillator(history, short=5, long=34):
    median_price = (history["High"] + history["Low"]) / 2
    short_sma = median_price.rolling(window=short).mean()
    long_sma = median_price.rolling(window=long).mean()
    awesome_oscillator = short_sma - long_sma
    return awesome_oscillator
def calculate_kst(history):
    close = history["Close"]
    roc1 = close.pct_change(10) * 100
    roc2 = close.pct_change(15) * 100
    roc3 = close.pct_change(20) * 100
    roc4 = close.pct_change(30) * 100
    rcma1 = roc1.rolling(window=10).mean()
    rcma2 = roc2.rolling(window=10).mean()
    rcma3 = roc3.rolling(window=10).mean()
    rcma4 = roc4.rolling(window=15).mean()
    kst = (rcma1 * 1) + (rcma2 * 2) + (rcma3 * 3) + (rcma4 * 4)
    return kst
def calculate_tsi(history, long=25, short=13):
    price_change = history["Close"].diff()
    abs_price_change = price_change.abs()
    double_smoothed_pc = price_change.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    double_smoothed_abs = abs_price_change.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    tsi = 100 * (double_smoothed_pc / double_smoothed_abs)
    return tsi
def calculate_cmo(history, period=14):
    change = history["Close"].diff()
    gains = change.where(change > 0, 0)
    losses = -change.where(change < 0, 0)
    sum_gains = gains.rolling(window=period).sum()
    sum_losses = losses.rolling(window=period).sum()
    
    cmo = 100 * ((sum_gains - sum_losses) / (sum_gains + sum_losses))
    return cmo
def calculate_dpo(history, period=20):
    shift = int(period / 2 + 1)
    sma = history["Close"].rolling(window=period).mean()
    dpo = history["Close"].shift(shift) - sma
    return dpo

def calculate_aroon(history, period=25):
    aroon_up = history["High"].rolling(window=period + 1).apply(lambda x: 100 * x.argmax() / period)
    aroon_down = history["Low"].rolling(window=period + 1).apply(lambda x: 100 * x.argmin() / period)
    aroon_oscillator = aroon_up - aroon_down
    return aroon_up, aroon_down, aroon_oscillator
def calculate_vortex(history, period=14):
    previous_close = history["Close"].shift(1)
    tr = history["High"].combine(previous_close, max) - history["Low"].combine(previous_close, min)
    vm_plus = (history["High"] - history["Low"].shift(1)).abs()
    vm_minus = (history["Low"] - history["High"].shift(1)).abs()
    tr_sum = tr.rolling(window=period).sum()
    
    vi_plus = vm_plus.rolling(window=period).sum() / tr_sum
    vi_minus = vm_minus.rolling(window=period).sum() / tr_sum
    return vi_plus, vi_minus
def calculate_obv(history):
    price_change = history["Close"].diff()
    direction = price_change.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (direction * history["Volume"]).cumsum()
    return obv
def calculate_adl(history):
    money_flow_multiplier = ((history["Close"] - history["Low"]) - (history["High"] - history["Close"])) / (history["High"] - history["Low"])
    money_flow_volume = money_flow_multiplier * history["Volume"]
    adl = money_flow_volume.cumsum()
    return adl
def calculate_cmf(history, period=20):
    money_flow_multiplier = ((history["Close"] - history["Low"]) - (history["High"] - history["Close"])) / (history["High"] - history["Low"])
    money_flow_volume = money_flow_multiplier * history["Volume"]
    cmf = money_flow_volume.rolling(window=period).sum() / history["Volume"].rolling(window=period).sum()
    return cmf
def calculate_force_index(history, period=13):
    
    force = history["Close"].diff() * history["Volume"]
    force_index = force.ewm(span=period, adjust=False).mean()
    return force_index
def calculate_eom(history, period=14):
    distance_moved = ((history["High"] + history["Low"]) / 2).diff()
    box_ratio = (history["Volume"] / 100000000) / (history["High"] - history["Low"])
    eom = distance_moved / box_ratio
    eom_smoothed = eom.rolling(window=period).mean()
    return eom_smoothed
def calculate_pvt(history):
    price_change_pct = history["Close"].pct_change()
    pvt = (price_change_pct * history["Volume"]).cumsum()
    return pvt
def calculate_nvi(history):
    price_change_pct = history["Close"].pct_change()
    nvi = pd.Series(1000.0, index=history.index)
    volume_change = history["Volume"].pct_change()
    nvi = nvi.where(volume_change <= 0, nvi.shift(1) * (1 + price_change_pct))
    return nvi
def calculate_vpt(history):
    price_change_pct = history["Close"].pct_change()
    vpt = (price_change_pct * history["Volume"]).cumsum()
    return vpt
def calculate_mfi(history, period=14):
    typical_price = (history["High"] + history["Low"] + history["Close"]) / 3
    money_flow = typical_price * history["Volume"]
    positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0)
    negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0)
    positive_sum = positive_flow.rolling(window=period).sum()
    negative_sum = negative_flow.rolling(window=period).sum()
    money_ratio = positive_sum / negative_sum
    mfi = 100 - (100 / (1 + money_ratio))
    return mfi
def calculate_williams_r(history, period=14):
    highest_high = history["High"].rolling(window=period).max()
    lowest_low = history["Low"].rolling(window=period).min()
    williams_r = -100 * ((highest_high - history["Close"]) / (highest_high - lowest_low))
    return williams_r
def calculate_roc(history, period=12):
    roc = ((history["Close"] / history["Close"].shift(period)) - 1) * 100
    return roc
def calculate_cci(history, period=20):
    typical_price = (history["High"] + history["Low"] + history["Close"]) / 3
    sma = typical_price.rolling(window=period).mean()
    mean_deviation = typical_price.rolling(window=period).apply(lambda x: (x - x.mean()).abs().mean())
    cci = (typical_price - sma) / (0.015 * mean_deviation)
    return cci

   
