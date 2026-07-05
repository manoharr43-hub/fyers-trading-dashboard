import streamlit as st
import pandas as pd

def market_summary(fyers):

    st.subheader("📊 Today's AI Market Summary")

    symbols = {
        "NIFTY 50": "NSE:NIFTY50-INDEX",
        "BANK NIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "INDIA VIX": "NSE:INDIAVIX-INDEX"
    }

    rows = []

    for name, symbol in symbols.items():

        try:
            q = fyers.quotes({"symbols": symbol})

            if q["s"] == "ok":

                d = q["d"][0]["v"]

                ltp = d.get("lp",0)
                openp = d.get("open_price",ltp)

                change = round(ltp-openp,2)

                if change > 0:
                    trend="🟢 UP"
                elif change <0:
                    trend="🔴 DOWN"
                else:
                    trend="🟡 NEUTRAL"

                rows.append({
                    "Index":name,
                    "LTP":ltp,
                    "Change":change,
                    "Trend":trend
                })

        except:
            pass

    df=pd.DataFrame(rows)

    st.dataframe(df,use_container_width=True)

    up=len(df[df.Trend=="🟢 UP"])
    down=len(df[df.Trend=="🔴 DOWN"])
    neutral=len(df[df.Trend=="🟡 NEUTRAL"])

    total=max(len(df),1)

    up_per=round(up/total*100)
    down_per=round(down/total*100)
    neu_per=round(neutral/total*100)

    c1,c2,c3=st.columns(3)

    c1.metric("📈 UP",f"{up_per}%")

    c2.metric("📉 DOWN",f"{down_per}%")

    c3.metric("⚖️ NEUTRAL",f"{neu_per}%")

    if up_per>60:
        st.success("🟢 AI VIEW : Market Bullish")

    elif down_per>60:
        st.error("🔴 AI VIEW : Market Bearish")

    else:
        st.warning("🟡 AI VIEW : Sideways / Neutral")
