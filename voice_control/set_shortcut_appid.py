"""Give the Start menu shortcut a stable Windows AppUserModelID."""

from pathlib import Path
import sys

from win32com.propsys import propsys


def main() -> None:
    path = str(Path(sys.argv[1]).resolve(strict=True))
    app_id = sys.argv[2]
    key = propsys.PSGetPropertyKeyFromName("System.AppUserModel.ID")
    store = propsys.SHGetPropertyStoreFromParsingName(path, None, 2, propsys.IID_IPropertyStore)
    store.SetValue(key, propsys.PROPVARIANTType(app_id))
    store.Commit()


if __name__ == "__main__":
    main()
