# besota
over-the-air firmware flasher for BES (Bestechnic) chips.

### supported chips
| SoC | status |
| :--- | :--- |
| bes2300p | ✅ mostly **supported** (tested on soundcore q35) |
| bes2300(a) | ❔ probably works but needs testing |
| bes1502x/p (aka bes2700*?) | ❔ unknown and needs testing |
| bes1600 (bes27***?) | ❔ unknown and needs testing |

### things and WIPs
| feature | status |
| :--- | :--- |
| BESOTA v1 (bes2300-2600? chips before 2021) | ✅ |
| BESOTA v2 (bes1600, 1502*, chips after 2021) | ❌ WIP |
| BT scanning on linux | ❌ not implemented (you can only connect with mac address) |
| support and the list of the chips | ❌ WIP |


## usage
by the way, this script is integrated into the openqore project. if you want to flash your headphones with custom firmware, please read the [openqore](https://github.com/nnonickreal/openqore) guides.

**note:** i'm not responsible for any damage caused as a result of incorrect use of this script or any bugs it may contain. this script was created with the assist of AI, but it has been tested on actual hardware (i use besota all the time).

if you want to experiment or do anything else, feel free to continue! c:

**1. download the script or clone the repository**

```
git clone https://github.com/nnonickreal/besota
```
**2. install [python](https://www.python.org/downloads/) and pybluez:**

```
pip install git+https://github.com/pybluez/pybluez.git
```

**3. launch the script**
you can just launch the script and follow instructions (pick your firmware, scan or connect to your headphones):
```
python besota.py
```

or e.g. on linux:
```
python besota.py firmware.bin --address aa:bb:cc:dd:ee:ff
```

run the script with the `-h` parameter to show available arguments.
