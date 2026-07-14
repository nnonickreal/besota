# besota
over-the-air firmware flasher for BES (Bestechnic) chips.

### supported chips
| SoC | status |
| :--- | :--- |
| bes2300p | ✅ mostly **supported** (tested on soundcore q35) |
| bes2300(a) | ❔ unknown and needs testing |
| bes1502x/p (aka bes2700IHC) | ❔ unknown and needs testing |
| bes1600 (bes2800IHC?) | ❔ unknown and needs testing |

## usage
by the way, this script is integrated into the openqore project. if you want to flash your headphones with custom firmware, please read the [openqore](https://github.com/nnonickreal/openqore) guides.

**note:** i'm not responsible for any damage caused as a result of incorrect use of this script or any bugs it may contain.

if you want to experiment or do anything else, feel free to continue! c:

**1. download the script or clone the repository**

```
git clone https://github.com/nnonickreal/besota
```
**2. install [python](https://www.python.org/downloads/)**

**3. launch the script**

```
python besota.py --mac 11:22:33:44:55:66 --port 1 ota_image.bin
```

run the script with the `--help` parameter to show available arguments.
