import streamlit as st
from fyers_apiv3 import fyersModel

# క్రెడెన్షియల్స్
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

def get_fyers_session():
    # సెషన్ మేనేజ్మెంట్
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )
    return session

def generate_token(auth_code):
    session = get_fyers_session()
    session.set_token(auth_code)
    response = session.generate_token()
    return response["access_token"]
