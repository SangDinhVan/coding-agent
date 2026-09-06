import os
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ["API_KEY"]
MODEL = "openai/lightning-ai/Qwen3.8-27B"
BASE_URL = "https://lightning.ai/api/v1/"


CONTEXT_WINDOW = 256000