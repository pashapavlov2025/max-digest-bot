"""Клавиатуры бота. Отдельный модуль, чтобы им могли пользоваться оба роутера."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from . import texts

# Постоянная клавиатура: без неё человек должен помнить команды наизусть
MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=texts.BUTTON_DAY), KeyboardButton(text=texts.BUTTON_THREE)],
        [KeyboardButton(text=texts.BUTTON_ASK), KeyboardButton(text=texts.BUTTON_PLAN)],
        [KeyboardButton(text=texts.BUTTON_SETTINGS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
