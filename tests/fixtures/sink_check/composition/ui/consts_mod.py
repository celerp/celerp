EVENT = "celerp:x"
GOOD_JS = f"document.addEventListener('{EVENT}', f);"
def _val(): return "x"
BAD_JS = f"var a={_val()};"
from dataclasses import dataclass
class Base: pass
@dataclass
class Line(Base):
    debit: int = 0
