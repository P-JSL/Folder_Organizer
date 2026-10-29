# Third-party software

Folder Organizer uses these libraries. Their licenses and source projects remain applicable to redistributed packages:

The application's own materials are subject to the restricted [Folder Organizer license](https://github.com/P-JSL/Folder_Organizer/blob/main/LICENSE): only P-JSL and explicitly authorized users may use those materials within their grant. This restriction does **not** replace or restrict the licenses of third-party components. The applicable LGPL rights to modify, replace and relink libraries, and reverse-engineer the combined work to debug such modifications, are preserved. No additional application permission is required to exercise rights independently granted for those components. In portable packages, the application license is at `LICENSE` and its terms, privacy notice and permission guidance are under `licenses/FolderOrganizer/`.

| Component | License / source |
| --- | --- |
| PySide6 / Qt 6 / Shiboken6 | LGPLv3/GPLv3 or commercial, https://doc.qt.io/qtforpython-6/licenses.html ; https://code.qt.io/cgit/pyside/pyside-setup.git/ ; https://code.qt.io/cgit/qt/ |
| Pillow | HPND (and bundled codec licenses), https://github.com/python-pillow/Pillow |
| pypdf | BSD-3-Clause, https://github.com/py-pdf/pypdf |
| Python | PSF license, https://www.python.org/psf/license/ |
| PyInstaller | GPL-2.0-or-later with bootloader exception, https://pyinstaller.org/en/stable/license.html |

The portable package uses PyInstaller's directory mode with dynamically loaded Qt libraries under `_internal`; keep these files with the executable. LGPLv3 and GPLv3 texts are included in `licenses/` (copied unmodified from the official Qt repository's [LGPL](https://github.com/qt/qtbase/blob/dev/LICENSES/LGPL-3.0-only.txt) and [GPL](https://github.com/qt/qtbase/blob/dev/LICENSES/GPL-3.0-only.txt) texts). Pillow and pypdf license notices are retained through the build's metadata collection. The source of this application is available in this repository. No modifications to these third-party libraries are made. You may replace compatible Qt shared libraries subject to their licenses. Consult the component sources for exact license terms, notices and source availability.
