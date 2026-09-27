# besota
over-the-air firmware flasher for BES (Bestechnic) chips.

### tested chips
| SoC | status |
| :--- | :--- |
| bes2300p | ✅ **tested** (on soundcore q35 & q30) |
| bes1502x/p (aka bes2700*?) | ✅ **tested** (CMF Headphone Pro & Soundcore Space One) |
| bes1600 (bes27***?) | ✅ **tested** (Nothing Headphone (1)) |
| bes2300(a) | ❔ probably works but needs testing |

### things and WIPs
| feature | status |
| :--- | :--- |
| BESOTA v1 (bes2300-2500? chips before 2021) | ✅ |
| BESOTA v1.1 (new chips with LZMA OTA support (2500-2800) | ✅ |
| GUI | ✅ |
| BESOTA v2 (new chips with LZMA OTA support (2500-2800) | ❌ WIP |
| BT scanning on linux | ❌ not implemented (you can only connect with mac address) |
| support and the list of the chips | ❌ WIP |

## ⚠ OTA_BOOT offset specifying, warnings & firmware

you need to specify the `OTA_BOOT` offset before flashing and download a firmware! see [firmware links](https://github.com/nnonickreal/openBES/blob/main/archive/FIRMWARES.md).

**do NOT even try to flash firmware from other headphones or any other file!**

the flasher includes some security checks, but they **don't** protect against third-party BES firmware! if you do this, there's a 99% chance you'll end up with a bricked device, which can only be fixed via UART and soldering. i am **not** responsible for any bricked devices.

if you want to experiment and have a UART access, feel free to continue!

## usage
by the way, this script is integrated into the openqore project. if you want to flash your headphones with custom firmware, please read the [openqore](https://github.com/nnonickreal/openqore) guides.

**1. download the [.exe file, script](https://github.com/nnonickreal/besota/releases/tag/v1.0.0-rc1) or clone the repository**

```
git clone https://github.com/nnonickreal/besota
```
**2. (building or cloning only) install [python](https://www.python.org/downloads/) and pybluez with pywebview:**

```
pip install git+https://github.com/pybluez/pybluez.git
pip install pywebview
```

**3. launch the executable, script or GUI**
you can just launch the script or GUI and follow instructions:
```
python besota_core.py
```
or
```
python besota_gui.py
```

or e.g. on linux:
```
python besota_core.py aa:bb:cc:dd:ee:ff firmware.bin
```

run the script with the `-h` parameter to show available arguments.
