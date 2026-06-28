"""
utils/fyers_api.py

FYERS V3 API Helper
"""

from fyers_apiv3 import fyersModel


class FyersAPI:

    def __init__(self, client_id, access_token):
        self.client_id = client_id
        self.access_token = access_token

        self.fyers = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.access_token,
            is_async=False,
            log_path=""
        )

    # -----------------------------------
    # Profile
    # -----------------------------------
    def get_profile(self):
        try:
            return self.fyers.get_profile()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Funds
    # -----------------------------------
    def get_funds(self):
        try:
            return self.fyers.funds()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Holdings
    # -----------------------------------
    def get_holdings(self):
        try:
            return self.fyers.holdings()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Positions
    # -----------------------------------
    def get_positions(self):
        try:
            return self.fyers.positions()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Orders
    # -----------------------------------
    def get_orders(self):
        try:
            return self.fyers.orderbook()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Trades
    # -----------------------------------
    def get_trades(self):
        try:
            return self.fyers.tradebook()
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Quotes
    # -----------------------------------
    def get_quote(self, symbol):
        try:
            return self.fyers.quotes({
                "symbols": symbol
            })
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Market Depth
    # -----------------------------------
    def get_depth(self, symbol):
        try:
            return self.fyers.depth({
                "symbol": symbol,
                "ohlcv_flag": "1"
            })
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Historical Data
    # -----------------------------------
    def get_history(
        self,
        symbol,
        resolution="5",
        date_format="1",
        range_from="2026-01-01",
        range_to="2026-12-31",
        cont_flag="1"
    ):
        try:
            data = {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": date_format,
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": cont_flag
            }

            return self.fyers.history(data)

        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Option Chain
    # -----------------------------------
    def get_option_chain(self, symbol, strikecount=10):
        try:
            return self.fyers.optionchain({
                "symbol": symbol,
                "strikecount": strikecount
            })
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Place Order
    # -----------------------------------
    def place_order(self, data):
        try:
            return self.fyers.place_order(data)
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Modify Order
    # -----------------------------------
    def modify_order(self, data):
        try:
            return self.fyers.modify_order(data)
        except Exception as e:
            return {"s": "error", "message": str(e)}

    # -----------------------------------
    # Cancel Order
    # -----------------------------------
    def cancel_order(self, order_id):
        try:
            return self.fyers.cancel_order({
                "id": order_id
            })
        except Exception as e:
            return {"s": "error", "message": str(e)}
