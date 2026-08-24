import os
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ["API_KEY"]
MODEL = "openai/deepseek/deepseek-v4-pro"
BASE_URL = "https://ai-gateway.inter-k.com/v1"

CONTEXT_WINDOW = 1_000_000