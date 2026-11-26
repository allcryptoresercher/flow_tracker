from aiogram.fsm.state import State, StatesGroup


class TrackSG(StatesGroup):
    coin = State()
    exchange = State()
    deposit = State()
    options = State()


class EditSG(StatesGroup):
    deposit = State()
