"""Клавиатуры бота. Отдельный модуль, чтобы им могли пользоваться оба роутера."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

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


def under_digest(chat_id: int, hours: int) -> InlineKeyboardMarkup:
    """
    Кнопки под сводкой — адрес следующего действия.

    Вопрос и просьба показать фото рождаются при чтении сводки конкретного
    чата, поэтому чат берётся отсюда, а не переспрашивается. Окно несём с
    собой: под сводкой за трое суток и фотографии показываем за трое суток.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BUTTON_ASK_CHAT, callback_data=f"ask:{chat_id}")],
            [InlineKeyboardButton(text=texts.BUTTON_PHOTOS, callback_data=f"pics:{chat_id}:{hours}")],
        ]
    )
