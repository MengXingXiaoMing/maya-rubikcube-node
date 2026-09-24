# rubikCube — Rubik's Cube Rig Node for Maya

A rigging node for Autodesk Maya that procedurally builds a customizable Rubik's cube. Every layer has its own controller, so the cube can be turned layer by layer like a real one.

![Maya](https://img.shields.io/badge/Maya-2025-blue) ![Python](https://img.shields.io/badge/Python-3-yellow) ![License](https://img.shields.io/badge/License-NonCommercial-red)

[中文](README.md) | English

<img src="https://cdn.jsdelivr.net/gh/MengXingXiaoMing/maya-rubikcube-node@main/docs/cube.png" alt="rubikCube 4x4x4" width="520">

## Features

- **Any size**: 3×3×3, 4×4×4, … up to 64×64×64 (`MAX_DIM` is configurable), and the three axes can differ (e.g. 3×4×5).
- **Per-layer controllers**: every layer on every axis gets its own controller, `Nx + Ny + Nz` in total, each rotating its own layer around its axis. A 3×3×3 has 9, a 4×4×4 has 12, an 18×18×18 has 54.
- **Master controller**: `rubikMaster` moves / rotates / scales the whole cube (all cubies and all layer controllers together).
- **Smart hiding**: while any controller is off a multiple of 90°, controllers on the other axes are hidden automatically, preventing layers on different axes from being operated at the same time; they reappear once every controller is back on a multiple of 90°.
- **90° snapping and commit**: a turn is only baked into the cube state when the controller lands exactly on a multiple of 90°; anything in between is a live preview only.
- **Automatic coloring**: cubies use the classic color scheme — +X red, -X orange, +Y white, -Y yellow, +Z green, -Z blue. Only faces that are actually exposed on the outside are colored; interior faces keep the default material.
- **Multiple cubes per scene**: build as many as you like — each new one gets a `_2`, `_3`, … suffix, so names never collide and the cubes stay independent.
- **Clean controllers**: controllers are square curves, colored per axis (X red / Y green / Z blue / master light grey), and keep only the rotation channel that axis needs — every other channel is locked and hidden.

## Requirements

- Autodesk Maya (developed and tested on **Maya 2025 / Python 3**)
- No third-party dependencies — only modules shipped with Maya: `maya.OpenMaya`, `maya.OpenMayaMPx`, `maya.cmds`
- Uses OpenMaya API 1.0; other Maya versions have not been tested individually

## Installation

1. Put `rubikCube.py` somewhere on Maya's plug-in search path (`MAYA_PLUG_IN_PATH`), or in any folder and load it by full path.
   - Example of the default Windows plug-in folder: `C:/Users/<user>/Documents/maya/2025/plug-ins/`
2. In Maya's **Script Editor → Python** tab:

```python
import maya.cmds as cmds
cmds.loadPlugin("rubikCube.py")
```

Or, if the file is not on the search path:

```python
cmds.loadPlugin(r"D:/maya_plugins/rubikCube.py")
```

## Quick start

```python
import maya.cmds as cmds

cmds.loadPlugin("rubikCube.py")

# Default: 3×3×3
cmds.rubikCube()

# 4×4×4
cmds.rubikCube(dimX=4, dimY=4, dimZ=4)

# Non-cubic (3×4×5) with custom spacing and cubie size
cmds.rubikCube(dimX=3, dimY=4, dimZ=5, spacing=1.2,
               cubieScale=0.95, controllerScale=1.0)
```

The command returns the name of the master controller:

```python
master = cmds.rubikCube(dimX=4, dimY=4, dimZ=4)
```

You can also call the build function directly to get all the parts:

```python
import rubikCube

master, node, controllers, cubies = rubikCube.buildRubikCube(4, 4, 4)
```

### Turning the cube

```python
# Turn layer 0 around the X axis by 90°
cmds.setAttr("rubikCtrl_X0.rotateX", 90)

# Turn the outermost Y layer by -90°
cmds.setAttr("rubikCtrl_Y2.rotateY", -90)

# Move / rotate the whole cube
cmds.setAttr("rubikMaster.translate", 0, 10, 0)
cmds.setAttr("rubikMaster.rotateY", 45)
```

You can also select controllers in the viewport and drag the rotate manipulator — the layer controller's rotation channel is the only unlocked channel.

### Removing the cube

```python
cmds.delete(cmds.ls("rubikMaster*", long=True))
```

## Build parameters

**Command `cmds.rubikCube(...)`**

| Short flag | Long flag | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `-dx` | `-dimX` | int | 3 | Number of cubies along X |
| `-dy` | `-dimY` | int | 3 | Number of cubies along Y |
| `-dz` | `-dimZ` | int | 3 | Number of cubies along Z |
| `-sp` | `-spacing` | float | 1.0 | Distance between cubie centers |
| `-cs` | `-cubieScale` | float | 1.0 | Cubie size relative to spacing (1.0 = touching) |
| `-cts` | `-controllerScale` | float | 1.0 | Scale of the layer controllers |

**Function `buildRubikCube(dimX, dimY, dimZ, spacing, cubie_scale, controller_scale)`**

Returns `(master_controller, node, layer_controllers, cubies)`.

## What gets created

For a default 3×3×3:

| Object | Naming | Notes |
| --- | --- | --- |
| Master controller | `rubikMaster` | Square curve, carries the overall transform |
| Cubies | `rubikCubie_0` … `rubikCubie_26` | Created in index order, matching the internal state |
| Layer controllers | `rubikCtrl_X0` … `rubikCtrl_Z2` | 3 per axis |
| Group nodes | `rubikCube_cubies`, `rubikCube_ctrls` | Parented under the master |
| Plug-in node | `rubikCubeNode1` | Computes and drives cubie positions and rotations |

A second cube gets a `_2` suffix on everything (`rubikMaster_2`, `rubikCubie_0_2`, `rubikCtrl_X0_2`, …), and so on.

Layer controllers sit in their own axis plane, are colored per axis, and keep only that axis' rotation channel:

<img src="https://cdn.jsdelivr.net/gh/MengXingXiaoMing/maya-rubikcube-node@main/docs/controllers.png" alt="One square controller per layer, colored per axis" width="520">

## How it works

### Why fixed scalar inputs instead of an array attribute

Layer angles use a fixed set of **scalar input attributes** `rotX0…rotZ63` (`MAX_DIM` per axis) rather than an array input attribute.

Reading an array input via `MArrayDataHandle` races with the output array evaluation under Maya's parallel Evaluation Manager, which in testing froze Maya. Fixed scalar inputs do not have this problem; the trade-off is that the maximum layer count is fixed by `MAX_DIM`, because attributes must be created when the plug-in is registered — Maya does not allow adding attributes at runtime.

`MAX_DIM` defaults to 64. The attribute count grows linearly with it (`3 × MAX_DIM` attributes in total), registration is cheap, so raise it as needed.

### State and commit

- Each cubie stores its grid coordinate `(px, py, pz)` and orientation quaternion `(qx, qy, qz, qw)`. Coordinates snap to multiples of 0.5 (integers for odd sizes, half-integers for even sizes).
- When a controller angle snaps to a multiple of 90°, a rotation of `delta = snapped - committed` is applied once to the cubies in that layer, and the committed value is updated — that is one "commit".
- Between snap points, only `raw - committed` is applied as a temporary display rotation, so the cube state is never polluted.
- All state lives in a single JSON string in the hidden attribute `cubeState`, avoiding a large number of per-cubie attributes.

### Outputs

The node exposes three array outputs, wired up by the build script:

| Attribute | Type | Connected to |
| --- | --- | --- |
| `outTranslate` | compound float3 array | each cubie's `translate` |
| `outRotate` | compound float3 array | each cubie's `rotate` |
| `outVisible` | bool array | each layer controller's `visibility` |

## FAQ

**`cannot be unloaded because it is still in use`**

The scene still contains nodes, or the undo queue still holds commands referencing the plug-in. Delete the objects and flush the undo queue first:

```python
cmds.delete(cmds.ls("rubikMaster*", long=True))
cmds.flushUndo()
for m in list(sys.modules):
    if m == "rubikCube" or m.startswith("rubikCube."):
        del sys.modules[m]
cmds.unloadPlugin("rubikCube.py")
```

**`尺寸 NxNxN 超过单轴最大层数 MAX_DIM=...`**

Raise `MAX_DIM` at the top of `rubikCube.py` (e.g. to 100) and reload the plug-in; the input attributes are registered automatically.

**Building large cubes is slow**

The cubie count grows with the cube of the size, and so does the build time (measured: ~2.9 s for 8×8×8, ~94 s for 18×18×18). The cost is in creating the cubies and connections, which is inherent to Maya's DG operations. Once built, turning stays smooth (an 18×18×18 evaluates a rotation in ~0.1 s).

**Why don't controllers use materials for their color?**

A NURBS curve's display color does not come from materials — it can only be set through drawing overrides. Controllers therefore use `overrideEnabled + overrideRGBColors + overrideColorRGB`, while cubies use ordinary lambert materials.

## Limitations

- The layer count per axis is capped at `MAX_DIM` (64 by default).
- Large sizes (18×18×18 and beyond) take a noticeable time to build and produce a very large number of objects.
- `cubeState` stores all state in one JSON string, which gets big at large sizes (~350 KB at 18³).

## License

Released under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) — see `LICENSE`.

- **Allowed**: personal learning, research, teaching, evaluation, and use or modification in noncommercial projects.
- **Not allowed**: any commercial use, including use in commercial products or services, selling, or paid distribution.

> Note: the source is public, but this is **not** an OSI-approved open-source license, because it restricts commercial use. For commercial licensing, please contact the author.

## Author

KangmingZhan
