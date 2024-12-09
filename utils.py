"""
utils.py

Functions:
- generate_script: Get the dialogue from the LLM.
- call_llm: Call the LLM with the given prompt and dialogue format.
- parse_url: Parse the given URL and return the text content.
- generate_podcast_audio: Generate audio for podcast using TTS or advanced audio models.
- _use_suno_model: Generate advanced audio using Bark.
- _use_melotts_api: Generate audio using TTS model.
- _get_melo_tts_params: Get TTS parameters based on speaker and language.
"""

# Standard library imports
import time
from typing import Any, Union

# Third-party imports
import instructor
import requests
from bark import SAMPLE_RATE, generate_audio, preload_models
from fireworks.client import Fireworks
from gradio_client import Client
from scipy.io.wavfile import write as write_wav

from openai import OpenAI
import json
from pydantic import BaseModel
from typing import List, Literal

#from TTS.api import TTS
import os

# Local imports
from constants import (
    FIREWORKS_API_KEY,
    FIREWORKS_MODEL_ID,
    FIREWORKS_MAX_TOKENS,
    FIREWORKS_TEMPERATURE,
    MELO_API_NAME,
    MELO_TTS_SPACES_ID,
    MELO_RETRY_ATTEMPTS,
    MELO_RETRY_DELAY,
    JINA_READER_URL,
    JINA_RETRY_ATTEMPTS,
    JINA_RETRY_DELAY,
    OPENAI_BASE_URL,
    OPENAI_MAX_TOKENS,
    OPENAI_MODEL_ID,
    OPENAI_TEMPERATURE
)
from schema import DialogueItem, ShortDialogue, MediumDialogue

# Initialize Fireworks client, with Instructor patch
if FIREWORKS_API_KEY is not None:
    fw_client = Fireworks(api_key=FIREWORKS_API_KEY)
    fw_client = instructor.from_fireworks(fw_client)

# Initialize Hugging Face client
hf_client = Client(MELO_TTS_SPACES_ID)

# Download and load all models for Bark
preload_models()


def generate_script(
    system_prompt: str,
    input_text: str,
    output_model: Union[ShortDialogue, MediumDialogue],
) -> Union[ShortDialogue, MediumDialogue]:
    """Get the dialogue from the LLM."""

    # Call the LLM for the first time
    first_draft_dialogue = call_llm(system_prompt, input_text, output_model)

    # Call the LLM a second time to improve the dialogue
    system_prompt_with_dialogue = f"{system_prompt}\n\nHere is the first draft of the dialogue you provided:\n\n{first_draft_dialogue.model_dump_json()}."
    final_dialogue = call_llm(system_prompt_with_dialogue, "Please improve the dialogue. Make it more natural and engaging. If there is a 'dialogue' element in the JSON already, extraxt it and use it as the text to improve. Always answer in the requested language!", output_model)

    return final_dialogue


def call_llm_fireworks(system_prompt: str, text: str, dialogue_format: Any) -> Any:
    """Call the LLM with the given prompt and dialogue format."""
    response = fw_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        model=FIREWORKS_MODEL_ID,
        max_tokens=FIREWORKS_MAX_TOKENS,
        temperature=FIREWORKS_TEMPERATURE,
        response_model=dialogue_format,
    )
    return response

def call_llm_openai(system_prompt: str, text: str, dialogue_format: Any) -> Any:
    """Call the local OpenAI-compatible LLM with the given prompt and dialogue format."""
    from openai import OpenAI

    # Initialisiere den OpenAI-Client mit der URL deines lokalen LLM-Servers
    client = OpenAI(base_url=OPENAI_BASE_URL, api_key="not-needed")

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        model=OPENAI_MODEL_ID,
        max_tokens=OPENAI_MAX_TOKENS,
        temperature=OPENAI_TEMPERATURE,
        #pydantic_function_tool=dialogue_format
    )

    return response

def call_llm(system_prompt: str, text: str, dialogue_format: type[BaseModel]) -> BaseModel:
    """Call the local OpenAI-compatible LLM with the given prompt and dialogue format."""
    from openai import OpenAI

    client = OpenAI(base_url=OPENAI_BASE_URL, api_key="not-needed")

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        model=OPENAI_MODEL_ID,
        max_tokens=OPENAI_MAX_TOKENS,
        temperature=OPENAI_TEMPERATURE,
        #pydantic_function_tool=dialogue_format
    )
    print("system_prompt Prompt:", system_prompt)
    print("Response:", response)

    # extract text
    if response.choices and len(response.choices) > 0:
        generated_text = response.choices[0].message.content
    else:
        raise ValueError("No response generated by the model")

    # try to parse as JSON
    try:
        print(f"Recieved generated_text: {generated_text}")
        response_data = json.loads(generated_text)
    except json.JSONDecodeError:
        # try to parse the text and see if it contains any JSON data and try to split at the first "{" and the last "}"
        json_start_index = generated_text.find('{')
        json_end_index = generated_text.rfind('}')
        if json_start_index != -1 and json_end_index != -1:
            snippet = generated_text[json_start_index:json_end_index+1]
            print(f"JSON data found, but could not be parsed. Trying to extract JSON from text: {snippet}")
            try:
                response_data = json.loads(snippet)
            except json.JSONDecodeError:
                print("Error parsing JSON snippet.")
                response_data = {
                    "scratchpad": "Keine strukturierten Gedanken verfügbar",
                    "name_of_guest": "Guest",
                    "dialogue": [{"speaker": "Host (Jane)", "text": generated_text}]
                }
        # still no valid JSON, create a standard dictionary
        else:
            print("No JSON data found. Creating a standard dictionary.")
            response_data = {
                "scratchpad": "Keine strukturierten Gedanken verfügbar",
                "name_of_guest": "Guest",
                "dialogue": [{"speaker": "Host (Jane)", "text": generated_text}]
            }

    if "scratchpad" not in response_data:
        response_data["scratchpad"] = "Nicht spezifiziert"
    if "name_of_guest" not in response_data:
        response_data["name_of_guest"] = "Guest"
    if "dialogue" not in response_data or not isinstance(response_data["dialogue"], list):
        response_data["dialogue"] = [{"speaker": "Host (Jane)", "text": str(response_data.get("dialogue", "Nicht spezifiziert"))}]

    valid_speakers = {'Host (Jane)', 'Guest'}
    for i, item in enumerate(response_data["dialogue"]):
        print(f"Item {i}: {item}")
        if not isinstance(item, dict):
            txt = str(item).strip()
            if txt == "":
                response_data["dialogue"][i] = {"speaker": "Host (Jane)", "text": "Nicht spezifiziert"}
            else:
                response_data["dialogue"][i] = {"speaker": "Host (Jane)", "text": str(item)}
        else:
            if "speaker" not in item or item["speaker"] not in valid_speakers:
                item["speaker"] = "Host (Jane)"
            if "text" not in item:
                item["text"] = "Nicht spezifiziert"

    try:
        formatted_response = dialogue_format(**response_data)
    except Exception as e:
        print(f"Error creating response model: {e}")
        print(f"Date recieved: {response_data}")
        raise

    return formatted_response

def parse_url(url: str) -> str:
    """Parse the given URL and return the text content."""
    for attempt in range(JINA_RETRY_ATTEMPTS):
        try:
            full_url = f"{JINA_READER_URL}{url}"
            response = requests.get(full_url, timeout=60)
            response.raise_for_status()  # Raise an exception for bad status codes
            break
        except requests.RequestException as e:
            if attempt == JINA_RETRY_ATTEMPTS - 1:  # Last attempt
                raise ValueError(
                    f"Failed to fetch URL after {JINA_RETRY_ATTEMPTS} attempts: {e}"
                ) from e
            time.sleep(JINA_RETRY_DELAY)  # Wait for X second before retrying
    return response.text


def generate_podcast_audio(
    text: str, speaker: str, language: str, use_advanced_audio: bool, random_voice_number: int
) -> str:
    """Generate audio for podcast using TTS or advanced audio models."""
    if use_advanced_audio:
        #return _use_coqui_tts(text, speaker, language, random_voice_number)
        return _use_suno_model(text, speaker, language, random_voice_number)
    else:
        return _use_melotts_api(text, speaker, language)

def _use_coqui_tts(text: str, speaker: str, language: str, random_voice_number: int) -> str:
    """Generate advanced audio using Coqui TTS."""
    # Sprachcode-Mapping
    language_code = {
        "english": "en",
        "german": "de",
        # Fügen Sie hier weitere Sprachen hinzu, falls benötigt
    }.get(language.lower(), "en")

    # Modellauswahl basierend auf der Sprache
    if language_code == "de":
        model_name = "tts_models/de/thorsten/tacotron2-DDC"
    else:
        model_name = "tts_models/en/ljspeech/tacotron2-DDC"

    # TTS-Instanz initialisieren
    tts = TTS(model_name=model_name)

    # Dateinamen generieren
    file_path = f"audio_{language}_{speaker}.wav"

    # Audio generieren und speichern
    tts.tts_to_file(text=text, file_path=file_path)

    return file_path

def _use_suno_model(text: str, speaker: str, language: str, random_voice_number: int) -> str:
    """Generate advanced audio using Bark."""
    host_voice_num = str(random_voice_number)
    guest_voice_num = str(random_voice_number + 1)
    audio_array = generate_audio(
        text,
        history_prompt=f"v2/{language}_speaker_{host_voice_num if speaker == 'Host (Jane)' else guest_voice_num}",
    )
    file_path = f"audio_{language}_{speaker}.mp3"
    write_wav(file_path, SAMPLE_RATE, audio_array)
    return file_path


def _use_melotts_api(text: str, speaker: str, language: str) -> str:
    """Generate audio using TTS model."""
    accent, speed = _get_melo_tts_params(speaker, language)

    for attempt in range(MELO_RETRY_ATTEMPTS):
        try:
            return hf_client.predict(
                text=text,
                language=language,
                speaker=accent,
                speed=speed,
                api_name=MELO_API_NAME,
            )
        except Exception as e:
            if attempt == MELO_RETRY_ATTEMPTS - 1:  # Last attempt
                raise  # Re-raise the last exception if all attempts fail
            time.sleep(MELO_RETRY_DELAY)  # Wait for X second before retrying


def _get_melo_tts_params(speaker: str, language: str) -> tuple[str, float]:
    """Get TTS parameters based on speaker and language."""
    if speaker == "Guest":
        accent = "EN-US" if language == "EN" else language
        speed = 0.9
    else:  # host
        accent = "EN-Default" if language == "EN" else language
        speed = (
            1.1 if language != "EN" else 1
        )  # if the language is not English, try speeding up so it'll sound different from the host
        # for non-English, there is only one voice
    return accent, speed
