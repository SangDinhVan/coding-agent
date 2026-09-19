import os
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ["API_KEY"]
MODEL = os.environ["MODEL"]
BASE_URL = os.environ["BASE_URL"]

CONTEXT_WINDOW = 256000