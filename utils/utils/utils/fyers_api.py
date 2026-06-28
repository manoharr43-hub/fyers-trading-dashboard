from fyers_apiv3 import fyersModel
import streamlit as st


class FyersAPI:
    def __init__(self):
        self.client_id = st.secrets["FYERS_CLIENT_ID"]
        self.token = st.session_state.get("access_token", "")

        self.fyers = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.token,
            is_async=False,
            log_path=""
        )

    # =========================
    # Profile
    # =========================
    def get_profile(self):
        return self.fyers.get_profile()

    # =========================
    # Funds
    # =========================
    def funds(self):
        return self.fyers.funds()

    # =========================
    # Holdings
    # =========================
    def holdings(self):
        return self.fyers.holdings()

    # =========================
    # Positions
    # =========================
    def positions(self):
        return self.fyers.positions()

    # =========================
    # Order Book
    # =========================
    def orderbook(self):
        return self.fyers.orderbook()

    # =========================
    # Trade Book
    # =========================
    def tradebook(self):
        return self.fyers.tradebook()

    # =========================
    # Quotes
    # =========================
    def quotes(self, symbols):
        return self.fyers.quotes({
            "symbols": symbols
        })

    # =========================
    # History
    # =========================
    def history(
        self,
        symbol,
        resolution,
        start_date,
        end_date
    ):
        data = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start_date,
            "range_to": end_date,
            "cont_flag": "1"
        }

        return self.fyers.history(data)

    # =========================
    # Market Depth
    # =========================
    def depth(self, symbol):
        return self.fyers.depth({
            "symbol": symbol,
            "ohlcv_flag": "1"
        })

    # =========================
    # Option Chain
    # =========================
    def option_chain(self, symbol, strikecount=10):
        data = {
            "symbol": symbol,
            "strikecount": strikecount,
            "timestamp": ""
        }

        return self.fyers.optionchain(data)

    # =========================
    # Place Order
    # =========================
    def place_order(self, data):
        return self.fyers.place_order(data)

    # =========================
    # Modify Order
    # =========================
    def modify_order(self, data):
        return self.fyers.modify_order(data)

    # =========================
    # Cancel Order
    # =========================
    def cancel_order(self, order_id):
        return self.fyers.cancel_order({
            "id": order_id
        })

    # =========================
    # Logout
    # =========================
    def logout(self):
        if "access_token" in st.session_state:
            del st.session_state["access_token"]
