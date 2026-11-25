"""Пикер смайликов: всплывающая панель с вкладками, вставка в поле ввода."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QPushButton, QScrollArea, QTabWidget,
                               QVBoxLayout, QWidget)

EMOJI = {
    "Смайлы": (
        "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 🙃 😉 😊 😇 🥰 😍 🤩 😘 😗 😚 😙 😋 😛 😜 🤪 😝 🤑 🤗 🤭 🤫 🤔 🫡 "
        "😐 😑 😶 😏 😒 🙄 😬 🤥 😌 😔 😪 😮‍💨 😴 😷 🤒 🤕 🤢 🤮 🤧 🥵 🥶 🥴 😵 🤯 🤠 🥳 🥸 😎 🤓 🧐 "
        "😕 😟 🙁 😮 😯 😲 😳 🥺 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 😞 😓 😩 😫 🥱 😤 😡 😠 🤬 😈 👿 💀 "
        "💩 🤡 👻 👽 👾 🤖 🎃 😺 😸 😹 😻 😼 😽 🙀 😿 😾 🫶 💋 💯 💥 💫 💦 💨 🕳 💬 💭 🕶"
    ),
    "Жесты": (
        "👋 🤚 🖐 ✋ 🖖 🫱 🫲 🫳 🫴 👌 🤌 🤏 ✌️ 🤞 🫰 🤟 🤘 🤙 👈 👉 👆 🖕 👇 ☝️ 🫵 👍 👎 ✊ 👊 🤛 🤜 "
        "👏 🙌 🫶 👐 🤲 🤝 🙏 💪 🦾 🖊 ✍️ 🙅 🙅‍♂️ 🙅‍♀️ 🙆 🙆‍♂️ 🙆‍♀️ 💁 💁‍♂️ 💁‍♀️ 🙋 🙋‍♂️ 🙋‍♀️ 🧏 🧏‍♂️ 🧏‍♀️ "
        "🙇 🙇‍♂️ 🙇‍♀️ 🤦 🤦‍♂️ 🤦‍♀️ 🤷 🤷‍♂️ 🤷‍♀️ 💆 💆‍♂️ 💆‍♀️ 💇 💇‍♂️ 💇‍♀️ 🚶 🚶‍♂️ 🚶‍♀️ 🏃 🏃‍♂️ 🏃‍♀️ 🧍 🧍‍♂️ 🧍‍♀️"
    ),
    "Люди": (
        "👶 🧒 👦 👧 🧑 👱 👨 👩 🧓 👴 👵 🧔 👨‍🦰 👩‍🦰 👨‍🦱 👩‍🦱 👨‍🦳 👩‍🦳 👨‍🦲 👩‍🦲 🧑‍🦰 🧑‍🦱 🧑‍🦳 🧑‍🦲 "
        "👲 👳‍♂️ 👳‍♀️ 🧕 👮 👮‍♂️ 👮‍♀️ 👷 👷‍♂️ 👷‍♀️ 💂 💂‍♂️ 💂‍♀️ 🕵️ 🕵️‍♂️ 🕵️‍♀️ 👩‍⚕️ 👨‍⚕️ 👩‍🌾 👨‍🌾 👩‍🍳 👨‍🍳 "
        "👩‍🎓 👨‍🎓 👩‍🎤 👨‍🎤 👩‍🏫 👨‍🏫 👩‍🏭 👨‍🏭 👩‍💻 👨‍💻 👩‍💼 👨‍💼 👩‍🔧 👨‍🔧 👩‍🔬 👨‍🔬 👩‍🎨 👨‍🎨 👩‍🚒 👨‍🚒 "
        "👩‍✈️ 👨‍✈️ 👩‍🚀 👨‍🚀 👩‍⚖️ 👨‍⚖️ 👰 🤵 👸 🤴 🥷 🦸 🦸‍♂️ 🦸‍♀️ 🦹 🦹‍♂️ 🦹‍♀️ 🧙 🧙‍♂️ 🧙‍♀️ 🧚 🧚‍♂️ 🧚‍♀️"
    ),
    "Животные": (
        "🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐽 🐸 🐵 🙈 🙉 🙊 🐒 🐔 🐧 🐦 🐤 🐣 🐥 🦆 🦅 🦉 "
        "🦇 🐺 🐗 🐴 🦄 🐝 🐛 🦋 🐌 🐞 🐜 🪲 🐢 🐍 🦎 🦂 🦀 🦞 🦐 🦑 🐙 🐡 🐠 🐟 🐬 🐳 🐋 🦈 "
        "🐊 🐅 🐆 🦓 🦍 🦧 🦣 🐘 🦛 🦏 🐪 🐫 🦒 🦘 🦬 🐃 🐂 🐄 🐎 🐖 🐏 🐑 🦙 🐐 🦌 🐕 🐩 🦮 "
        "🐕‍🦺 🐈 🐈‍⬛ 🪶 🐓 🦃 🦤 🦚 🦜 🦢 🦩 🕊 🐇 🦝 🦨 🦡 🦫 🦦 🦥 🐁 🐀 🦔"
    ),
    "Еда": (
        "🍏 🍎 🍐 🍊 🍋 🍌 🍉 🍇 🍓 🫐 🍈 🍒 🍑 🥭 🍍 🥥 🥝 🍅 🫒 🍆 🥑 🥦 🥬 🥒 🌶 🫑 🌽 🥕 🫒 "
        "🧄 🧅 🥔 🍠 🥐 🥯 🍞 🥖 🥨 🧀 🥚 🍳 🧈 🥞 🧇 🥓 🥩 🍗 🍖 🦴 🌭 🍔 🍟 🍕 🫓 🥪 🥙 🧆 🌮 "
        "🌯 🫔 🥗 🥘 🫕 🥫 🍝 🍜 🍲 🍛 🍣 🍱 🥟 🦪 🍤 🍙 🍚 🍘 🍥 🥠 🥮 🍢 🍡 🍧 🍨 🍦 🥧 🧁 🍰 "
        "🎂 🍮 🍭 🍬 🍫 🍿 🍩 🍪 🌰 🥜 🍯 🥛 🍼 🫖 ☕ 🍵 🧃 🥤 🧋 🍶 🍺 🍻 🥂 🍷 🥃 🍸 🍹 🧉 🍾"
    ),
    "Символы": (
        "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💟 ☮️ ✝️ ☪️ 🕉 ☸️ ✡️ 🔯 🕎 ☯️ ☦️ "
        "🛐 ⛎ ♈ ♉ ♊ ♋ ♌ ♍ ♎ ♏ ♐ ♑ ♒ ♓ 🆔 ⚛️ 🉑 ☢️ ☣️ 📴 📳 🈶 🈚 🈸 🈺 🈷️ ✴️ 🆚 💮 🉐 ㊙️ ㊗️ "
        "🈴 🈵 🈹 🈲 🅰️ 🅱️ 🆎 🆑 🅾️ 🆘 ❌ ⭕ 🛑 ⛔ 📛 🚫 💯 💢 ♨️ 🚷 🚯 🚳 🚱 🔞 📵 🚭 ❗ ❕ ❓ ❔ "
        "‼️ ⁉️ 🔅 🔆 〽️ ⚠️ 🚸 🔱 ⚜️ 🔰 ♻️ ✅ 🈯 💹 ❇️ ✳️ ❎ 🌐 💠 Ⓜ️ 🌀 💤 🏧 ♿ 🚹 🚺 🚻 🚮 🛗 "
        "🅿️ 🛗 🚾 🛂 🛃 🛄 🛅 🚹 🚺 🚼 ⚧ 🚻 🚮 🎦 📶 🈁 🆖 🆗 🆙 🆒 🆕 🆓 0️⃣ 1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣ 🔟"
    ),
    "Предметы": (
        "🎉 🎊 🎈 🎁 🎀 🎗 🎟 🎫 🏆 🥇 🥈 🥉 ⚽ 🏀 🏈 ⚾ 🥎 🎾 🏐 🏉 🥏 🎱 🪀 🏓 🏸 🏒 🏑 🥍 🏏 🪃 "
        "🥅 ⛳ 🪁 🏹 🎣 🤿 🥊 🥋 🎽 🛹 🛼 🛷 ⛸ 🥌 🎿 ⛷ 🏂 🪂 🏋️ 🤼 🤸 ⛹️ 🤺 🤾 🏌️ 🏇 🧘 🏄 🏊 🤽 "
        "🚣 🧗 🚵 🚴 🏆 🥇 🎖 🏅 🎫 🎪 🤹 🎭 🩰 🎨 🎬 🎤 🎧 🎼 🎹 🥁 🪘 🎷 🎺 🪗 🎸 🪕 🎻 🎲 ♟ 🎯 "
        "🎳 🎮 🎰 🧩 🚗 🚕 🚙 🚌 🚎 🏎 🚓 🚑 🚒 🚐 🛻 🚚 🚛 🚜 🦯 🦽 🦼 🛴 🚲 🛵 🏍 🛺 🚨 🚔 🚍 🚘 "
        "🚖 🚡 🚠 🚟 🚃 🚋 🚞 🚝 🚄 🚅 🚈 🚂 🚆 🚇 🚊 🚉 ✈️ 🛫 🛬 🛩 💺 🛰 🚀 🛸 🚁 🛶 ⛵ 🚤 🛥 🛳"
    ),
}


class EmojiPicker(QWidget):
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("emojiPicker")
        self.setStyleSheet(
            "#emojiPicker { background: #1e1f22; border: 1px solid #111214; border-radius: 8px; }"
            "QPushButton { background: transparent; border: none; font-size: 15pt; padding: 2px; }"
            "QPushButton:hover { background: #35373c; border-radius: 4px; }"
            "QTabBar::tab { padding: 6px 10px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        tabs = QTabWidget()
        for name, chars in EMOJI.items():
            tabs.addTab(self._page(chars.split()), name)
        lay.addWidget(tabs)
        self.resize(430, 320)

    def _page(self, emojis: list[str]) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(0)
        grid.setContentsMargins(2, 2, 2, 2)
        cols = 9
        for i, e in enumerate(emojis):
            b = QPushButton(e)
            b.setFixedSize(42, 42)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, e=e: self._pick(e))
            grid.addWidget(b, i // cols, i % cols)
        area.setWidget(inner)
        return area

    def _pick(self, e: str):
        self.picked.emit(e)
        self.close()


class EmojiButton(QPushButton):
    """Кнопка 😊 в поле ввода: открывает пикер над собой."""

    def __init__(self, on_emoji):
        super().__init__("😊")
        self.setObjectName("attach")
        self.setFixedSize(44, 44)
        self.setToolTip("Смайлики")
        self.setCursor(Qt.PointingHandCursor)
        self._on_emoji = on_emoji
        self._picker: EmojiPicker | None = None
        self.clicked.connect(self._open)

    def _open(self):
        if self._picker is None:
            self._picker = EmojiPicker(self)
            self._picker.picked.connect(self._on_emoji)
        p = self.mapToGlobal(self.rect().topLeft())
        self._picker.move(p.x() - 180, p.y() - self._picker.height() - 8)
        self._picker.show()
        self._picker.raise_()
