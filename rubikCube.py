# -*- coding: utf-8 -*-
"""
魔方绑定节点 (rubikCubeNode) —— 可调尺寸版
============================================

功能
----
可调长宽高（dimX × dimY × dimZ）的魔方绑定节点，配合 `buildRubikCube()` 构建脚本使用：

- 每一层（沿每个轴的每一个切片）都有一个控制器，绕该轴旋转该层。例如 3×3×3
  有 3+3+3=9 个层控制器（不只是 6 个面），4×4×4 有 12 个。
- 总控控制器 `rubikMaster` 控制整个魔方（所有小方块 + 所有层控制器一起移动/旋转）。
- 当某个轴的控制器数值不是 90° 的整数倍时，其它轴的控制器自动隐藏，
  此时只能操作当前轴的各层控制器。
- 只有所有控制器都回到 90° 的整数倍时，其它轴的控制器才重新显示。
- 节点内部记录"已提交"旋转状态：只有控制器到达 90° 整数倍时，才会把这次转动
  固化（提交）到魔方状态中；未到达整数倍时仅做临时预览旋转。
- 小方块按经典魔方配色自动上色：+X 红、-X 橙、+Y 白、-Y 黄、+Z 绿、-Z 蓝，
  只有暴露在外表面的面才着色，内部面保持默认材质。
- 同一场景可重复构建多个魔方，后建的自动加 `_2`、`_3` ... 后缀，互不重名。

设计说明
--------
- 每个小方块记录：网格坐标 (px, py, pz) + 方向四元数 (qx, qy, qz, qw)。
  坐标为"居中坐标"：奇数维是整数（3 维 -> -1/0/1），偶数维是半整数
  （4 维 -> -1.5/-0.5/0.5/1.5），统一吸附到 0.5 的倍数。
- 提交(commit)：控制器角度吸附到 90° 整数倍时，用 `delta = 吸附值 - 已提交值`
  对当前处于该层切片内的方块执行一次旋转，然后更新已提交值。
- 显示(display)：未吸附时，把 `raw - 已提交值` 作为临时增量旋转显示出来。
- 状态以 JSON 字符串形式存在隐藏属性 `cubeState` 中，避免大量散列属性。

关于输入属性的说明（重要）
--------------------------
- 层控制器角度不使用 Maya 的数组输入属性，而是固定数量的标量属性
  `rotX0..rotZ{MAX_DIM-1}`。数组输入属性在并行求值(EM)下读取
  `MArrayDataHandle` 会与输出数组求值产生竞争，导致 Maya 卡死；
  固定标量输入与旧版（已验证不卡死）的结构一致。
- 因此每个轴支持的层数上限由 `MAX_DIM` 决定（当前 64，即最大 64×64×64）。
  属性数量随 `MAX_DIM` 线性增长（每个轴 MAX_DIM 个），注册开销很小；
  实际可构建的尺寸主要受性能限制：方块数按立方增长（8³=512、18³=5832），
  构建时间与每次求值开销都会随之显著上升。

用法
----
    import maya.cmds as cmds
    cmds.loadPlugin('rubikCube.py')
    import rubikCube
    rubikCube.buildRubikCube(4, 4, 4)   # 4×4×4

    # 或使用命令：
    cmds.rubikCube(dimX=4, dimY=4, dimZ=4)
"""

# py2.7/py3 双兼容说明：不要加 from __future__ import unicode_literals——
# Maya 2019 及更早的 py2 SWIG 绑定（MFnPlugin、MFn*Attribute.create 等
# char const* 入参）只接受字节串，全量 unicode 会导致构造/注册失败。
# 策略：普通字面量保持字节串（C++ 边界天然正确）；凡要与 cmds/MEL 返回
# 的 unicode 拼接、或含中文要正确显示的字符串，显式加 u"" 前缀。
import sys
import math
import json

import maya.OpenMaya as om
import maya.OpenMayaMPx as ompx
import maya.cmds as cmds
import maya.mel as mel

# 判断"是否吸附到 90° 整数倍"的容差（单位：度）
SNAP_EPS = 0.01

# 默认尺寸
DEFAULT_DIMS = (3, 3, 3)

# cubeState JSON 字典的 key。必须用 u"" 前缀：py2.7 下 json.loads 返回的
# 字典 key 是 unicode，用普通字节串 key 取值会 KeyError；py3 下 u"" 与
# 普通字符串完全等价。
KEY_CUBIES = u"cubies"
KEY_COMMITTED = u"committed"
KEY_LAST_EULER = u"lastEuler"

# 每个轴最多支持的层数（即最大支持 MAX_DIM × MAX_DIM × MAX_DIM）。
# 由于层控制器角度使用固定标量输入，此值决定了需要预先注册的输入属性数量
# （每个轴 MAX_DIM 个，共 3 × MAX_DIM 个输入属性）。属性数量线性增长、注册
# 开销很小，所以这里给得比较宽松；实际可构建尺寸受性能限制（方块数按立方增长）。
MAX_DIM = 64

# 六个外表面的材质颜色（经典魔方配色）：方向 -> (材质名, RGB)
FACE_COLORS = {
    "posX": ("rubikMat_posX", (0.85, 0.10, 0.10)),  # 红
    "negX": ("rubikMat_negX", (0.95, 0.45, 0.05)),  # 橙
    "posY": ("rubikMat_posY", (0.95, 0.95, 0.95)),  # 白
    "negY": ("rubikMat_negY", (0.95, 0.85, 0.10)),  # 黄
    "posZ": ("rubikMat_posZ", (0.05, 0.55, 0.20)),  # 绿
    "negZ": ("rubikMat_negZ", (0.05, 0.30, 0.75)),  # 蓝
}

# polyCube 面索引与方向的对应关系（实测：f0=+Z f1=+Y f2=-Z f3=-Y f4=+X f5=-X）
FACE_INDEX = {
    "posX": 4, "negX": 5,
    "posY": 1, "negY": 3,
    "posZ": 0, "negZ": 2,
}

# 控制器颜色（视口显示色，走 drawing override）：同一轴上的层控制器同色
CTRL_COLORS = {
    "master": (0.85, 0.85, 0.85),  # 浅灰
    "cubie": (1.00, 0.75, 0.20),  # 橙黄：单个小方块的小控制器
    "X": (0.90, 0.25, 0.25),  # 红：绕 X 轴旋转的层
    "Y": (0.25, 0.80, 0.35),  # 绿：绕 Y 轴旋转的层
    "Z": (0.30, 0.50, 0.95),  # 蓝：绕 Z 轴旋转的层
}

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _axis_quat(axis, degrees):
    """返回绕 axis(0=X / 1=Y / 2=Z) 旋转 degrees 度的单位四元数。"""
    rad = math.radians(degrees)
    if axis == 0:
        e = om.MEulerRotation(rad, 0.0, 0.0)
    elif axis == 1:
        e = om.MEulerRotation(0.0, rad, 0.0)
    else:
        e = om.MEulerRotation(0.0, 0.0, rad)
    return e.asQuaternion()


def _snap90(value):
    """把角度吸附到最近的 90° 整数倍。"""
    return round(value / 90.0) * 90.0


def _unwrap_angle(current, previous):
    """把 current 平移到与 previous 差值在 (-180, 180] 内的角度。

    asEulerRotation 返回值始终在 [-180, 180]，连续旋转时会在 ±180 边界
    骤变（例如 179 -> 180 -> -179）。unwrap 用"最近整数圈"修正，
    让输出连续累加，不出现跳变。previous 为 None 时不做处理。
    """
    if previous is None:
        return current
    return current - round((current - previous) / 360.0) * 360.0


def _snap_coord(value):
    """把网格坐标吸附到最近的 0.5（奇数维为整数、偶数维为半整数）。"""
    return round(value * 2.0) / 2.0


def _cubie_count(dims):
    return dims[0] * dims[1] * dims[2]


def _slice_count(dims):
    return dims[0] + dims[1] + dims[2]


def _home_indices(dims, index):
    """由小方块序号反推其网格索引 (ix, iy, iz)，顺序与构建脚本一致。"""
    dx, dy, dz = dims
    iz = index // (dx * dy)
    rem = index % (dx * dy)
    iy = rem // dx
    ix = rem % dx
    return ix, iy, iz


def _home_position(dims, index):
    """返回第 index 个小方块的初始居中坐标 (x, y, z)。"""
    dx, dy, dz = dims
    ix, iy, iz = _home_indices(dims, index)
    return (ix - (dx - 1) / 2.0,
            iy - (dy - 1) / 2.0,
            iz - (dz - 1) / 2.0)


def _axis_of(k, dims):
    """第 k 个层控制器所属的轴（0=X / 1=Y / 2=Z）。"""
    dx, dy, dz = dims
    if k < dx:
        return 0
    if k < dx + dy:
        return 1
    return 2


def _axis_local_index(k, dims):
    """第 k 个层控制器在所属轴上的局部序号 (axis, local_index)。"""
    dx, dy, dz = dims
    if k < dx:
        return 0, k
    k -= dx
    if k < dy:
        return 1, k
    k -= dy
    return 2, k


def _slice_coord(k, dims):
    """第 k 个层控制器对应的 (轴索引, 切片坐标)。"""
    dx, dy, dz = dims
    if k < dx:
        return 0, (k - (dx - 1) / 2.0)
    k -= dx
    if k < dy:
        return 1, (k - (dy - 1) / 2.0)
    k -= dy
    return 2, (k - (dz - 1) / 2.0)


def _controller_name(axis, k, dims, tag=""):
    """生成层控制器名字，例如 rubikCtrl_X0 / rubikCtrl_Y1 / rubikCtrl_Z2。"""
    dx, dy, dz = dims
    if axis == 0:
        idx = k
    elif axis == 1:
        idx = k - dx
    else:
        idx = k - dx - dy
    return "rubikCtrl_%s%d%s" % ("XYZ"[axis], idx, tag)


def _next_cube_tag():
    """返回本次构建可用的名字后缀，保证同场景多魔方互不重名。

    第一个魔方使用原始名字（rubikMaster / rubikCubie_0 ...），之后依次
    追加 _2、_3 ...，避免 Maya 在重名时把名字末尾数字自动递增
    （例如 rubikCtrl_X0 被改成 rubikCtrl_X1），从而错连到别的轴上。
    """
    i = 1
    while True:
        tag = "" if i == 1 else "_%d" % i
        if not (cmds.objExists("rubikMaster" + tag)
                or cmds.objExists("rubikCubie_0" + tag)):
            return tag
        i += 1


def _default_state(dims):
    """生成初始魔方状态。

    cubies 每个元素为 [px, py, pz, qx, qy, qz, qw]；
    committed 为每个层控制器上一次"已提交"的角度（长度 = 层数）。
    """
    cubies = []
    for i in range(_cubie_count(dims)):
        x, y, z = _home_position(dims, i)
        cubies.append([x, y, z, 0.0, 0.0, 0.0, 1.0])
    # lastEuler: 每个方块上一次输出的欧拉角 [ex, ey, ez]，
    # 用于 unwrap 防骤变（asEulerRotation 在 ±180 边界会跳变）
    last_euler = [[0.0, 0.0, 0.0] for _ in range(_cubie_count(dims))]
    return {KEY_CUBIES: cubies,
            KEY_COMMITTED: [0.0] * _slice_count(dims),
            KEY_LAST_EULER: last_euler}


# ---------------------------------------------------------------------------
# 旋转 / 显示 / 可见性逻辑
# ---------------------------------------------------------------------------
def _commit(state, dims, raws):
    """根据各层控制器的原始角度，对魔方状态执行 90° 增量提交。

    只有某个控制器吸附到 90° 整数倍时，才把 `吸附值 - 已提交值` 的增量旋转
    固化到状态中；否则视为"进行中"，不提交。
    """
    committed = state[KEY_COMMITTED]
    for k in range(len(raws)):
        raw = raws[k]
        snapped = _snap90(raw)
        if abs(raw - snapped) >= SNAP_EPS:
            continue  # 未吸附，暂不提交

        delta = snapped - committed[k]
        if abs(delta) <= 1e-6:
            continue  # 与上次提交值一致，无需处理

        axis, coord = _slice_coord(k, dims)
        q_move = _axis_quat(axis, delta)

        for c in state[KEY_CUBIES]:
            px, py, pz = c[0], c[1], c[2]
            if (px, py, pz)[axis] != coord:
                continue  # 不在该层切片内

            v = om.MVector(px, py, pz).rotateBy(q_move)
            c[0] = _snap_coord(v.x)
            c[1] = _snap_coord(v.y)
            c[2] = _snap_coord(v.z)

            q_old = om.MQuaternion(c[3], c[4], c[5], c[6])
            q_new = q_old * q_move
            q_new.normalizeIt()
            c[3] = q_new.x
            c[4] = q_new.y
            c[5] = q_new.z
            c[6] = q_new.w

        committed[k] = snapped


def _active_axis(dims, raws):
    """返回当前"进行中"的轴索引（0=X / 1=Y / 2=Z）。

    若所有控制器都吸附到 90° 整数倍，返回 None。
    多个轴同时进行时只取第一个（保证"同时只能动一个轴"）。
    """
    for k in range(len(raws)):
        if abs(raws[k] - _snap90(raws[k])) >= SNAP_EPS:
            return _axis_of(k, dims)
    return None


def _compute_display(state, dims, raws, unwrap=True):
    """根据"已提交状态 + 进行中的旋转"计算所有小方块的平移与欧拉旋转。

    输出平移为网格居中坐标（单位网格空间）；轴向排列间距由 compute
    在写出前统一按分量相乘（spacingX/Y/Z），旋转提交/进行中逻辑全部
    在网格空间完成，不接触间距。

    unwrap=True 时输出欧拉角使用 unwrap 修正：以 state["lastEuler"] 为基准，
    把 asEulerRotation 返回的 [-180,180] 值平移到与上次差值在 (-180,180]，
    使连续旋转时的读数单调累加，不在 ±180 边界骤变。
    unwrap=False 时直接输出原始欧拉角（范围 [-180, 180]）。
    """
    committed = state[KEY_COMMITTED]
    active_axis = _active_axis(dims, raws)

    # lastEuler 缓存：旧状态文件可能没有这个字段，这里做兼容
    last_euler = state.get(KEY_LAST_EULER)
    n = len(state[KEY_CUBIES])
    if last_euler is None or len(last_euler) != n:
        last_euler = [[0.0, 0.0, 0.0] for _ in range(n)]
        state[KEY_LAST_EULER] = last_euler

    # 收集进行中的控制器：只收集当前活动轴（同一轴各层），保证"只能动一个轴"。
    in_progress = []
    for k in range(len(raws)):
        if active_axis is not None and _axis_of(k, dims) != active_axis:
            continue
        raw = raws[k]
        if abs(raw - _snap90(raw)) >= SNAP_EPS:
            in_progress.append((k, raw - committed[k]))

    translates = []
    rotates = []
    for idx, c in enumerate(state[KEY_CUBIES]):
        px, py, pz = c[0], c[1], c[2]
        q = om.MQuaternion(c[3], c[4], c[5], c[6])
        pos = om.MVector(px, py, pz)

        for k, angle in in_progress:
            axis, coord = _slice_coord(k, dims)
            if (px, py, pz)[axis] == coord:
                q_move = _axis_quat(axis, angle)
                pos = pos.rotateBy(q_move)
                q = q * q_move

        q.normalizeIt()
        euler = q.asEulerRotation()
        ex = math.degrees(euler.x)
        ey = math.degrees(euler.y)
        ez = math.degrees(euler.z)

        if unwrap:
            # unwrap：让本次输出尽可能接近上一次，避免 ±180 边界跳变
            prev = last_euler[idx]
            ex = _unwrap_angle(ex, prev[0])
            ey = _unwrap_angle(ey, prev[1])
            ez = _unwrap_angle(ez, prev[2])
            prev[0], prev[1], prev[2] = ex, ey, ez
        else:
            # 不 unwrap 时仍更新缓存（保证重新开启时基准不漂移）
            prev = last_euler[idx]
            prev[0], prev[1], prev[2] = ex, ey, ez

        translates.append((pos.x, pos.y, pos.z))
        rotates.append((ex, ey, ez))

    return translates, rotates


def _compute_visibility(dims, raws):
    """计算各层控制器的可见性。

    存在"进行中"（未吸附）的控制器时，只显示该轴的所有层控制器；否则全部显示。
    """
    active_axis = _active_axis(dims, raws)
    return [active_axis is None or _axis_of(k, dims) == active_axis
            for k in range(len(raws))]


# ---------------------------------------------------------------------------
# 节点类
# ---------------------------------------------------------------------------
class RubikCubeNode(ompx.MPxNode):
    kNodeName = "rubikCubeNode"
    kNodeId = om.MTypeId(0x00141488)

    # 输入属性
    dimX = om.MObject()
    dimY = om.MObject()
    dimZ = om.MObject()
    spacingX = om.MObject()
    spacingY = om.MObject()
    spacingZ = om.MObject()
    cubeState = om.MObject()
    unwrapEuler = om.MObject()

    # 各层控制器角度：固定标量输入 (axis, local_index) -> MObject。
    # 不使用数组输入，避免并行求值(EM)下 MArrayDataHandle 与输出数组竞争导致卡死。
    rotAttrs = {}

    # 输出属性
    outTranslate = om.MObject()
    outTranslateX = om.MObject()
    outTranslateY = om.MObject()
    outTranslateZ = om.MObject()
    outRotate = om.MObject()
    outRotateX = om.MObject()
    outRotateY = om.MObject()
    outRotateZ = om.MObject()
    outRotateCache = om.MObject()
    outRotateCacheX = om.MObject()
    outRotateCacheY = om.MObject()
    outRotateCacheZ = om.MObject()
    outVisible = om.MObject()

    def __init__(self):
        super(RubikCubeNode, self).__init__()

    # ------------------------------------------------------------------
    def compute(self, plug, dataBlock):
        attr = plug.attribute()
        if (attr != self.outTranslate and attr != self.outRotate
                and attr != self.outRotateCache
                and attr != self.outVisible):
            return om.kUnknownParameter

        dx = max(1, dataBlock.inputValue(self.dimX).asInt())
        dy = max(1, dataBlock.inputValue(self.dimY).asInt())
        dz = max(1, dataBlock.inputValue(self.dimZ).asInt())
        dims = (dx, dy, dz)
        # 轴向排列间距：网格坐标乘以各轴间距得到最终世界位置。
        # 旋转/提交逻辑始终在网格坐标空间进行，间距只影响显示位置。
        sx = dataBlock.inputValue(self.spacingX).asDouble()
        sy = dataBlock.inputValue(self.spacingY).asDouble()
        sz = dataBlock.inputValue(self.spacingZ).asDouble()
        slice_n = _slice_count(dims)
        cubie_n = _cubie_count(dims)

        # 读取 unwrap 选项（默认开启）
        unwrap = dataBlock.inputValue(self.unwrapEuler).asBool()
        # 旋转缓存开关：由总控 rubikCache 连接驱动。开启时 outRotateCache
        # 输出连续 unwrap 的累计旋转（供骨骼侧旋转缓存组直连，修正约束
        # 输出的 ±180 翻转）；关闭时输出归零，退回纯约束模式。
        cache_on = dataBlock.inputValue(self.cacheEnable).asBool()

        # 1. 读取各层控制器角度（固定标量输入）
        raws = []
        for k in range(slice_n):
            axis, local_idx = _axis_local_index(k, dims)
            raws.append(dataBlock.inputValue(self.rotAttrs[(axis, local_idx)]).asFloat())

        # 2. 读取状态（JSON 字符串，直接写回 cubeState，与旧版一致）
        state_str = dataBlock.inputValue(self.cubeState).asString()
        try:
            state = json.loads(state_str)
        except Exception:
            state = None
        if (state is None
                or len(state.get(KEY_CUBIES, [])) != cubie_n
                or len(state.get(KEY_COMMITTED, [])) != slice_n):
            state = _default_state(dims)

        # 3. 执行提交
        _commit(state, dims, raws)

        # 4. 计算显示结果与可见性
        translates, rotates = _compute_display(state, dims, raws, unwrap)
        vis = _compute_visibility(dims, raws)
        # 网格坐标 -> 世界坐标：按轴乘以排列间距（旋转结果也在此缩放）
        translates = [(x * sx, y * sy, z * sz) for x, y, z in translates]

        # 5. 写回状态
        dataBlock.inputValue(self.cubeState).setString(json.dumps(state))

        # 6. 写出输出
        _write_compound_array(dataBlock, self.outTranslate,
                              self.outTranslateX, self.outTranslateY, self.outTranslateZ,
                              translates)
        _write_compound_array(dataBlock, self.outRotate,
                              self.outRotateX, self.outRotateY, self.outRotateZ,
                              rotates)
        # 旋转缓存输出：与 outRotate 同值（连续 unwrap），cache 关闭时归零
        rot_cache = rotates if cache_on else [(0.0, 0.0, 0.0)] * len(rotates)
        _write_compound_array(dataBlock, self.outRotateCache,
                              self.outRotateCacheX, self.outRotateCacheY,
                              self.outRotateCacheZ, rot_cache)
        _write_bool_array(dataBlock, self.outVisible, vis)

        dataBlock.setClean(plug)
        return

    # ------------------------------------------------------------------
    @classmethod
    def nodeInitializer(cls):
        nAttr = om.MFnNumericAttribute()
        tAttr = om.MFnTypedAttribute()

        # --- 输入：尺寸 ---
        for name, short in (("dimX", "dmx"), ("dimY", "dmy"), ("dimZ", "dmz")):
            a = nAttr.create(name, short, om.MFnNumericData.kInt, 3)
            nAttr.setMin(1)
            nAttr.setKeyable(True)
            nAttr.setWritable(True)
            nAttr.setStorable(True)
            setattr(cls, name, a)
            cls.addAttribute(a)

        # --- 输入：三个轴的排列间距（默认 1.0，由总控自定义属性驱动） ---
        for name, short in (("spacingX", "spx"),
                            ("spacingY", "spy"),
                            ("spacingZ", "spz")):
            a = nAttr.create(name, short, om.MFnNumericData.kDouble, 1.0)
            nAttr.setMin(0.01)
            nAttr.setKeyable(False)
            nAttr.setWritable(True)
            nAttr.setStorable(True)
            setattr(cls, name, a)
            cls.addAttribute(a)

        # --- 输入：各层控制器旋转角度（固定标量，每个轴 MAX_DIM 个） ---
        cls.rotAttrs = {}
        for axis in range(3):
            for idx in range(MAX_DIM):
                long_name = "rot%s%d" % ("XYZ"[axis], idx)
                short_name = "r%s%d" % ("xyz"[axis], idx)
                a = nAttr.create(long_name, short_name, om.MFnNumericData.kFloat, 0.0)
                nAttr.setKeyable(False)
                nAttr.setWritable(True)
                nAttr.setStorable(True)
                cls.rotAttrs[(axis, idx)] = a
                cls.addAttribute(a)

        # --- 隐藏状态：魔方状态 JSON 字符串 ---
        cls.cubeState = tAttr.create("cubeState", "cst", om.MFnData.kString)
        default_str = om.MFnStringData().create(json.dumps(_default_state(DEFAULT_DIMS)))
        tAttr.setDefault(default_str)
        tAttr.setHidden(True)
        tAttr.setStorable(True)
        tAttr.setWritable(True)
        tAttr.setKeyable(False)
        tAttr.setConnectable(False)
        cls.addAttribute(cls.cubeState)

        # --- 选项：是否启用欧拉角 unwrap 缓存（默认开启） ---
        # 开启时输出欧拉角连续累加，不在 ±180 边界骤变；
        # 关闭时直接输出 asEulerRotation 的原始值（范围 [-180, 180]）
        cls.unwrapEuler = nAttr.create("unwrapEuler", "ue", om.MFnNumericData.kBoolean, 1)
        nAttr.setKeyable(True)
        nAttr.setWritable(True)
        nAttr.setStorable(True)
        cls.addAttribute(cls.unwrapEuler)

        # --- 选项：缓存播放开关输入（由总控 rubikCache 连接驱动） ---
        # 一线两用：① compute 里门控 outRotateCache 输出（骨骼旋转缓存）；
        # ② Node Editor 可见连线端点。实际切换 Maya 缓存播放由总控属性
        # 回调完成（缓存播放是全局状态，无法被 connectAttr）。
        cls.cacheEnable = nAttr.create("cacheEnable", "ce", om.MFnNumericData.kBoolean, 1)
        nAttr.setKeyable(False)
        nAttr.setWritable(True)
        nAttr.setStorable(True)
        cls.addAttribute(cls.cacheEnable)

        # --- 输出：平移（复合 3 float 数组） ---
        cls.outTranslateX = nAttr.create("outTranslateX", "otx", om.MFnNumericData.kFloat, 0.0)
        cls.outTranslateY = nAttr.create("outTranslateY", "oty", om.MFnNumericData.kFloat, 0.0)
        cls.outTranslateZ = nAttr.create("outTranslateZ", "otz", om.MFnNumericData.kFloat, 0.0)
        cls.outTranslate = nAttr.create("outTranslate", "ot",
                                        cls.outTranslateX, cls.outTranslateY, cls.outTranslateZ)
        nAttr.setArray(True)
        nAttr.setUsesArrayDataBuilder(True)
        nAttr.setWritable(False)
        nAttr.setStorable(False)
        cls.addAttribute(cls.outTranslate)

        # --- 输出：旋转（复合 3 float 数组） ---
        cls.outRotateX = nAttr.create("outRotateX", "orx", om.MFnNumericData.kFloat, 0.0)
        cls.outRotateY = nAttr.create("outRotateY", "ory", om.MFnNumericData.kFloat, 0.0)
        cls.outRotateZ = nAttr.create("outRotateZ", "orz", om.MFnNumericData.kFloat, 0.0)
        cls.outRotate = nAttr.create("outRotate", "orr",
                                     cls.outRotateX, cls.outRotateY, cls.outRotateZ)
        nAttr.setArray(True)
        nAttr.setUsesArrayDataBuilder(True)
        nAttr.setWritable(False)
        nAttr.setStorable(False)
        cls.addAttribute(cls.outRotate)

        # --- 输出：旋转缓存（复合 3 float 数组，受 cacheEnable 门控） ---
        # 与 outRotate 同源同值（连续 unwrap 的累计旋转），但 cacheEnable
        # 为 False 时输出归零。骨骼侧的旋转缓存骨骼（rubikJointRot_i）直连
        # 此属性：约束两侧的累计旋转互相抵消，关节本地只剩小控制器偏移，
        # 读数不再 ±180 翻转；关闭缓存则退回纯约束模式。
        cls.outRotateCacheX = nAttr.create("outRotateCacheX", "orcx",
                                           om.MFnNumericData.kFloat, 0.0)
        cls.outRotateCacheY = nAttr.create("outRotateCacheY", "orcy",
                                           om.MFnNumericData.kFloat, 0.0)
        cls.outRotateCacheZ = nAttr.create("outRotateCacheZ", "orcz",
                                           om.MFnNumericData.kFloat, 0.0)
        cls.outRotateCache = nAttr.create("outRotateCache", "orc",
                                          cls.outRotateCacheX,
                                          cls.outRotateCacheY,
                                          cls.outRotateCacheZ)
        nAttr.setArray(True)
        nAttr.setUsesArrayDataBuilder(True)
        nAttr.setWritable(False)
        nAttr.setStorable(False)
        cls.addAttribute(cls.outRotateCache)

        # --- 输出：可见性（bool 数组，长度 = 层数） ---
        cls.outVisible = nAttr.create("outVisible", "ovs", om.MFnNumericData.kBoolean, True)
        nAttr.setArray(True)
        nAttr.setUsesArrayDataBuilder(True)
        nAttr.setWritable(False)
        nAttr.setStorable(False)
        cls.addAttribute(cls.outVisible)

        # --- 依赖关系 ---
        for name in ("dimX", "dimY", "dimZ"):
            cls.attributeAffects(getattr(cls, name), cls.outTranslate)
            cls.attributeAffects(getattr(cls, name), cls.outRotate)
            cls.attributeAffects(getattr(cls, name), cls.outRotateCache)
            cls.attributeAffects(getattr(cls, name), cls.outVisible)
        for sname in ("spacingX", "spacingY", "spacingZ"):
            cls.attributeAffects(getattr(cls, sname), cls.outTranslate)
        for axis in range(3):
            for idx in range(MAX_DIM):
                a = cls.rotAttrs[(axis, idx)]
                cls.attributeAffects(a, cls.outTranslate)
                cls.attributeAffects(a, cls.outRotate)
                cls.attributeAffects(a, cls.outRotateCache)
                cls.attributeAffects(a, cls.outVisible)
        cls.attributeAffects(cls.unwrapEuler, cls.outTranslate)
        cls.attributeAffects(cls.unwrapEuler, cls.outRotate)
        cls.attributeAffects(cls.unwrapEuler, cls.outRotateCache)
        cls.attributeAffects(cls.cacheEnable, cls.outRotateCache)

    # ------------------------------------------------------------------
    @classmethod
    def nodeCreator(cls):
        return ompx.asMPxPtr(RubikCubeNode())


# ---------------------------------------------------------------------------
# 输出写入辅助
# ---------------------------------------------------------------------------
def _write_compound_array(dataBlock, array_attr, child_x, child_y, child_z, values):
    arr = dataBlock.outputArrayValue(array_attr)
    builder = arr.builder()
    for i, (x, y, z) in enumerate(values):
        h = builder.addElement(i)
        h.child(child_x).setFloat(x)
        h.child(child_y).setFloat(y)
        h.child(child_z).setFloat(z)
    arr.set(builder)
    arr.setAllClean()


def _write_bool_array(dataBlock, array_attr, values):
    arr = dataBlock.outputArrayValue(array_attr)
    builder = arr.builder()
    for i, v in enumerate(values):
        h = builder.addElement(i)
        h.setBool(v)
    arr.set(builder)
    arr.setAllClean()


# ---------------------------------------------------------------------------
# 构建脚本
# ---------------------------------------------------------------------------
def _outer_faces(ix, iy, iz, dims):
    """返回位于魔方表面的小方块需要上色的方向列表。

    只有当方块处于某个轴的最外层时，该方向的面才露在外面需要着色。
    """
    dx, dy, dz = dims
    keys = []
    if ix == 0:
        keys.append("negX")
    if ix == dx - 1:
        keys.append("posX")
    if iy == 0:
        keys.append("negY")
    if iy == dy - 1:
        keys.append("posY")
    if iz == 0:
        keys.append("negZ")
    if iz == dz - 1:
        keys.append("posZ")
    return keys


def _get_shading_group(name, rgb):
    """创建（或复用）指定名称与颜色的材质，返回其 shading group 名。

    材质在所有魔方之间共享，因此只创建一次。
    """
    if not cmds.objExists(name):
        mat = cmds.shadingNode("lambert", asShader=True, name=name)
        cmds.setAttr(mat + ".color", rgb[0], rgb[1], rgb[2], type="double3")
    sg = name + "SG"
    if not cmds.objExists(sg):
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg)
        cmds.connectAttr(name + ".outColor", sg + ".surfaceShader", f=True)
    return sg


def _set_controller_color(ctrl, rgb):
    """用 drawing override 给控制器（NURBS 曲线）上色。

    曲线不参与材质着色，显示颜色只能由 override 控制；这里用 RGB 覆盖
    以获得精确颜色，而不是调色板索引。
    """
    cmds.setAttr(ctrl + ".overrideEnabled", 1)
    cmds.setAttr(ctrl + ".overrideRGBColors", 1)
    cmds.setAttr(ctrl + ".overrideColorRGB", rgb[0], rgb[1], rgb[2], type="double3")


def _square_curve(size, axis, name):
    """创建一个闭合的正方形曲线控制器，法线方向为 axis 对应的轴。"""
    h = size / 2.0
    if axis == 0:      # 法线 X：正方形位于 YZ 平面
        corners = [(0.0, -h, -h), (0.0, h, -h), (0.0, h, h), (0.0, -h, h)]
    elif axis == 1:    # 法线 Y：正方形位于 XZ 平面
        corners = [(-h, 0.0, -h), (h, 0.0, -h), (h, 0.0, h), (-h, 0.0, h)]
    else:              # 法线 Z：正方形位于 XY 平面
        corners = [(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)]
    return cmds.curve(degree=1, point=corners + [corners[0]], name=name)


def _create_master_controller(dims, tag=""):
    """创建总控控制器：一个位于原点的水平正方形。"""
    size = max(dims) * 1.7
    master = _square_curve(size, 1, "rubikMaster" + tag)
    cmds.setAttr(master + ".translate", 0, 0, 0)
    cmds.setAttr(master + ".rotate", 0, 0, 0)
    _set_controller_color(master, CTRL_COLORS["master"])
    return master


def _create_slice_controller(axis, scale, name):
    """在原点创建一个正方形层控制器（单位基准尺寸，边长 1.4*scale）。

    位置与两个面内轴向的实际尺寸由外层 offset 组驱动（随排列间距变化），
    控制器自身只保留旋转通道。
    """
    ctrl = _square_curve(1.4 * scale, axis, name)
    cmds.setAttr(ctrl + ".translate", 0, 0, 0)
    cmds.setAttr(ctrl + ".rotate", 0, 0, 0)
    return ctrl


def _cubie_gizmo_curve(size, name):
    """创建小方块控制器的 gizmo 曲线：单个形状节点、一笔连续 degree-1 曲线。

    形状（3D 版参考图）：
      - 六个面（±X/±Y/±Z）的面心各一个小方框，框内画叉（两条对角线）
      - 三条轴线穿过正方体中心，连接两两相对的面框中心
    构造方式是一笔连续路径：每面图案从面心起笔、收笔回面心，面与面之间
    路径穿过正方体中心。一次 MFnNurbsCurve.create 只产生一个 nurbsCurve
    形状节点（不能用多条曲线 parent/拼凑，那样会是多个 shape）。
    """
    h = size * 0.5   # 面心到原点的距离
    a = size * 0.2   # 每个小方框的半边长
    # 六个面按相对面成对排列：(面心, 面内第一根轴, 面内第二根轴)
    faces = (
        (( h, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ((-h, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ((0.0,  h, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ((0.0, -h, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ((0.0, 0.0,  h), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ((0.0, 0.0, -h), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    )

    def corner(c, u, v, su, sv):
        return (c[0] + a * (su * u[0] + sv * v[0]),
                c[1] + a * (su * u[1] + sv * v[1]),
                c[2] + a * (su * u[2] + sv * v[2]))

    # 整条曲线是一笔连续路径（degree-1，无任何断点跳变）：
    # 每个面的图案从面心 C 起笔、画方框+叉后收笔回 C；面与面之间路径
    # 经过正方体中心，画出连接各面心的三条轴线。
    # 单个面的笔顺（允许描重）：
    #   C -> p00 -> p10 -> p11 -> p01 -> p00 -> C   方框 + 回到面心
    #   -> p11 -> C -> p01 -> p10 -> C              两条对角叉
    # 相邻 CV 之间的每条线都是方框边、叉或轴线，不会出现多余连线。
    # 注意：不能用"共享 knot 跳变跨"拼独立段——跳变两侧 CV 不重合时
    # Maya 视口仍会画连接线（arclen 查不出来）。
    walk = []
    origin = (0.0, 0.0, 0.0)
    for c, u, v in faces:
        p00 = corner(c, u, v, -1, -1)
        p10 = corner(c, u, v,  1, -1)
        p11 = corner(c, u, v,  1,  1)
        p01 = corner(c, u, v, -1,  1)
        if walk:
            walk.append(origin)  # 上一面心 -> 正方体中心 -> 本面心
        walk.extend([c, p00, p10, p11, p01, p00, c, p11, c, p01, p10, c])

    points = om.MPointArray()
    knots = om.MDoubleArray()
    for i, q in enumerate(walk):
        points.append(om.MPoint(q[0], q[1], q[2]))
        knots.append(float(i))
    fn = om.MFnNurbsCurve()
    transform_obj = fn.create(points, knots, 1,
                              om.MFnNurbsCurve.kOpen, False, False)
    om.MFnDagNode(transform_obj).setName(name)
    return name


def _create_cubie_controller(name, size):
    """创建单个小方块的小控制器：6 面方框+叉+中心连线的 gizmo。

    TRS 全部保留可动画：本地平移/旋转与节点输出相加后驱动骨骼，
    本地缩放通过 scaleConstraint 驱动骨骼。
    """
    ctrl = _cubie_gizmo_curve(size, name)
    cmds.setAttr(ctrl + ".translate", 0, 0, 0)
    cmds.setAttr(ctrl + ".rotate", 0, 0, 0)
    cmds.setAttr(ctrl + ".scale", 1, 1, 1)
    _set_controller_color(ctrl, CTRL_COLORS["cubie"])
    return ctrl


def _setup_slice_controller(ctrl, axis):
    """按轴给层控制器上色，并只保留该轴的旋转通道。

    必须在大纲层级(parent)确定之后调用：锁定 translate 会阻止后续
    的父子补偿操作。
    """
    _set_controller_color(ctrl, CTRL_COLORS["XYZ"[axis]])

    keep = "rotate%s" % "XYZ"[axis]
    for attr in ("translateX", "translateY", "translateZ",
                 "rotateX", "rotateY", "rotateZ",
                 "scaleX", "scaleY", "scaleZ"):
        if attr == keep:
            continue
        cmds.setAttr("%s.%s" % (ctrl, attr), lock=True, keyable=False, channelBox=False)
    # visibility 由节点输出驱动（自动隐藏其它轴的控制器），只隐藏不锁定
    cmds.setAttr(ctrl + ".visibility", keyable=False, channelBox=False)
    # 唯一保留的旋转通道：可打关键帧（keyable 属性会自动显示在通道盒）
    # 注意 keyable 与 channelBox 互斥，不能同时设置
    cmds.setAttr("%s.%s" % (ctrl, keep), lock=False, keyable=True)
    return ctrl


def _setup_spacing(master, node, cubie_scale, cubie_makers, tag):
    """在总控上添加统一的间距属性 rubikSpacing 并完成接线。

    总控只暴露一个"间距 (Spacing)"标量，一条连接同时驱动节点的三个
    间距输入（X/Y/Z 统一缩放）。节点的 spacingX/Y/Z 仍是独立输入，
    需要非均匀间距时可在 Node Editor 里断开单轴手调。
    数据流：
      master.rubikSpacing -> node.spacingX/Y/Z （骨骼/控制器位置按网格坐标缩放）
                          -> 三个共享 multiplyDivide （x cubie_scale）
                             -> 所有 polyCube 的 width/height/depth
                             （方块网格在蒙皮上游缩放，骨骼自身缩放不变）
    返回 {0: multX, 1: multY, 2: multZ}，层控制器 offset 组复用这三个
    间距输出驱动非均匀缩放。
    """
    # 旧版三个分轴属性（rubikSpacingX/Y/Z）：存在则删除，连接随属性断开
    for ch in "XYZ":
        if cmds.attributeQuery("rubikSpacing%s" % ch, node=master, exists=True):
            cmds.deleteAttr("%s.rubikSpacing%s" % (master, ch))
    # 注意短名不能用 sp：与 transform 内置 scalePivot 冲突的是 spx 等，
    # 这里用 rsp 规避一切内置短名
    if not cmds.attributeQuery("rubikSpacing", node=master, exists=True):
        cmds.addAttr(master, longName="rubikSpacing", shortName="rsp",
                     attributeType="double", defaultValue=1.0,
                     minValue=0.01, maxValue=10.0,
                     niceName="间距 (Spacing)", keyable=True)
    else:
        cmds.addAttr(master + ".rubikSpacing", e=True, keyable=True)

    dim_mults = {}
    for axis, maker_attr in enumerate(("width", "height", "depth")):
        ch = "XYZ"[axis]
        cmds.connectAttr("%s.rubikSpacing" % master,
                         "%s.spacing%s" % (node, ch), force=True)
        mult = cmds.createNode("multiplyDivide",
                               name="rubikCubieDim%s%s" % (ch, tag))
        cmds.setAttr(mult + ".operation", 1)  # multiply
        cmds.setAttr(mult + ".input1%s" % ch, float(cubie_scale))
        cmds.connectAttr("%s.rubikSpacing" % master,
                         mult + ".input2%s" % ch, force=True)
        for maker in cubie_makers:
            cmds.connectAttr(mult + ".output%s" % ch,
                             "%s.%s" % (maker, maker_attr))
        dim_mults[axis] = mult
    return dim_mults


# ---------------------------------------------------------------------------
# 总控"开启缓存"属性 -> Maya 缓存播放（Cached Playback）联动
# ---------------------------------------------------------------------------
# MNodeMessage / MSceneMessage 回调 id：保模块引用防止被 GC；
# 插件卸载时 Maya 会自动移除插件注册的所有回调。
_CACHE_ATTR_CBS = []
_SCENE_OPEN_CB = []


def _apply_cached_playback(enabled):
    """开启/关闭 Maya 缓存播放（时间轴右下角的缓存切换）。

    走官方偏好接口 CachePreferenceEnabled().set_value：一条命令同时完成
    1) 写 cachedPlaybackEnable optionVar（跨 Maya 会话持久化）；
    2) 应用运行时 cacheEvaluator 状态（关闭时自动 flush 已缓存数据）；
    3) 刷新时间轴上的缓存按钮图标。
    等价于用户点击时间轴的"缓存播放切换"按钮。
    """
    try:
        from maya.plugin.evaluator.cache_preferences import CachePreferenceEnabled
        CachePreferenceEnabled().set_value(bool(enabled))
    except Exception:
        try:
            om.MGlobal.displayWarning(
                "rubikCube: 无法切换缓存播放，cacheEvaluator 插件不可用"
                " (failed to toggle cached playback)")
        except Exception:
            pass


def _on_master_attr_changed(msg, plug, other_plug, client_data):
    """总控属性变化回调：rubikCache 被修改 -> 切换缓存播放。

    回调在主线程 setAttr 命令内同步触发，此处读到的 plug 值已是新值。
    注意：maya.OpenMaya 是 SWIG 绑定，不支持关键字参数，partialName
    必须按位置传参 (includeNodeName, includeNonMandatoryIndices,
    includeInstanceName, useAlias, useFullAttributePath, useLongNames)。
    """
    try:
        if plug.partialName(False, False, False, False, False, True) == "rubikCache":
            _apply_cached_playback(plug.asBool())
    except Exception:
        pass


def _register_master_cache_callback(master):
    """给指定总控挂属性变化回调（构建与场景打开后各挂一次）。"""
    try:
        sel = om.MSelectionList()
        sel.add(master)
        # OpenMaya 1.0 老式出参签名：getDependNode(index, MObject)，
        # 不会把 MObject 作为返回值
        mobj = om.MObject()
        sel.getDependNode(0, mobj)
        cb = om.MNodeMessage.addAttributeChangedCallback(
            mobj, _on_master_attr_changed, None)
        _CACHE_ATTR_CBS.append(cb)
    except Exception:
        pass


def _on_scene_opened(*_args):
    """场景打开后重新挂接总控缓存回调并应用已保存的属性值。

    MNodeMessage 回调不随场景保存，重开场景后需要重建；此时若
    rubikCache 为 True 则恢复开启缓存，与场景保存时的意图一致。
    """
    try:
        for master in (cmds.ls("rubikMaster*", type="transform") or []):
            if not cmds.attributeQuery("rubikCache", node=master, exists=True):
                continue
            _register_master_cache_callback(master)
            if cmds.getAttr(master + ".rubikCache"):
                _apply_cached_playback(True)
    except Exception:
        pass


def _setup_master_cache(master, node):
    """总控"开启缓存 (Cache)"开关：连接节点 + 联动 Maya 缓存播放。

    1) master 加 rubikCache 属性（默认开启），connectAttr 真实连到
       rubikCubeNode.cacheEnable（Node Editor 可见连线，参考
       connectAttr -f rubikMaster.rubikCache rubikCubeNode1.unwrapEuler）；
    2) 实际切换缓存由属性变化回调调用官方偏好接口完成——缓存开关是
       optionVar/evaluator 全局状态，没有可被 connectAttr 的目标属性，
       官方时间轴按钮也是这么做的；
    3) 默认开启：构建时应用一次 True。
    """
    if not cmds.attributeQuery("rubikCache", node=master, exists=True):
        cmds.addAttr(master, longName="rubikCache", shortName="rcb",
                     attributeType="bool", defaultValue=True,
                     niceName="开启缓存 (Cache)", keyable=True)
    else:
        # 旧场景升级：历史版本建的属性不可见（无 keyable），提升到通道栏
        cmds.addAttr(master + ".rubikCache", e=True, keyable=True)
    cmds.connectAttr(master + ".rubikCache", node + ".cacheEnable", force=True)
    _register_master_cache_callback(master)
    # 默认开启缓存（defaultValue=True 的运行态应用）
    _apply_cached_playback(bool(cmds.getAttr(master + ".rubikCache")))


def _setup_master_gizmo(master, node, dims, tag):
    """总控曲线形状随间距自动缩放（几何层，不占 scale 通道）。

    与小控制器 gizmo 同一套方案：源曲线（intermediate 不可见）经
    transformGeometry 按 spacingX/Y/Z 缩放后喂给总控曲线 shape 的
    .create。总控的 scale 通道必须保持纯净——它经 parentConstraint
    + scaleConstraint 驱动总骨骼；间距缩放若走 scale 会与用户缩放
    混在一起且无法单独撤销。间距默认 1 时矩阵为单位阵，形状与旧版
    完全一致；间距 (sx, sy, sz) 时正方形沿 X/Z 拉伸 sx/sz 倍
    （总控画在 XZ 平面，Y 无关），与方块布局同步变大变小。
    """
    size = max(dims) * 1.7
    src = _square_curve(size, 1, "rubikMasterSrc" + tag)
    cmds.parent(src, "rubikMaster_grp" + tag)
    src_shp = cmds.listRelatives(src, shapes=True, fullPath=True)[0]
    cmds.setAttr(src_shp + ".intermediateObject", 1)
    mat = cmds.createNode("composeMatrix", name="rubikMasterScale" + tag)
    cmds.connectAttr(node + ".spacingX", mat + ".inputScaleX")
    cmds.connectAttr(node + ".spacingY", mat + ".inputScaleY")
    cmds.connectAttr(node + ".spacingZ", mat + ".inputScaleZ")
    tg = cmds.createNode("transformGeometry", name="rubikMasterTg" + tag)
    cmds.connectAttr(src_shp + ".local", tg + ".inputGeometry")
    cmds.connectAttr(mat + ".outputMatrix", tg + ".transform")
    mshp = cmds.listRelatives(master, shapes=True, fullPath=True)[0]
    cmds.connectAttr(tg + ".outputGeometry", mshp + ".create")


def resetCube(node):
    """一键还原魔方到初始状态：重置内部 cubeState + 归零所有层控制器。

    用途：当多个控制器被随意修改后统一归零，内部 committed 状态可能与
    实际不一致，导致方块位置错乱。此函数直接重置 cubeState 为 _default_state，
    并把所有层控制器的旋转角度归零，保证魔方回到构建时的初始状态。

    参数
    ----
    node : rubikCubeNode 节点名（也可传 master 名，会自动反查）
    """
    # 接受节点名或 master 名
    if cmds.nodeType(node) != "rubikCubeNode":
        # 尝试从 master 名推 tag 再找节点
        tag = ""
        if node.startswith("rubikMaster"):
            tag = node[len("rubikMaster"):]
        cand = "rubikCubeNode1" + tag
        if cmds.objExists(cand) and cmds.nodeType(cand) == "rubikCubeNode":
            node = cand
        else:
            raise ValueError(u"找不到 rubikCubeNode 节点: %s" % node)

    dx = cmds.getAttr(node + ".dimX")
    dy = cmds.getAttr(node + ".dimY")
    dz = cmds.getAttr(node + ".dimZ")
    dims = (dx, dy, dz)

    # 1. 重置内部状态（cubeState 写回初始值）
    cmds.setAttr(node + ".cubeState",
                 json.dumps(_default_state(dims)), type="string")

    # 2. 归零所有层控制器
    #    outRotate 经过 unitConversion 才到控制器，反查会拿到 unitConversion；
    #    改用 outVisible[k] -> ctrl.visibility 反查控制器名（无中间转换节点）
    for k in range(_slice_count(dims)):
        axis, coord = _slice_coord(k, dims)
        # 通过 outVisible 连接找到控制器（outVisible[k] -> ctrl.visibility）
        vis_dst = (cmds.listConnections(node + ".outVisible[%d]" % k,
                                        s=False, d=True, plugs=True) or [])
        ctrl = None
        if vis_dst:
            ctrl = vis_dst[0].split(".")[0]
        if ctrl and cmds.objExists(ctrl):
            channel = "rotate" + "XYZ"[axis]
            cmds.setAttr(ctrl + "." + channel, 0.0)

    # cmds.select(clear=True)
    print(u"魔方已还原到初始状态: %s" % node)
    return node


def buildRubikCube(dimX=3, dimY=3, dimZ=3,
                   cubie_scale=1.0, controller_scale=1.0):
    """构建一个可调长宽高的魔方绑定。

    参数
    ----
    dimX/dimY/dimZ  : 三个方向的方块数量（默认 3×3×3，可传 4×4×4 等；
                      每个轴最大为 MAX_DIM）
    cubie_scale     : 单个小方块的边长（默认 1.0；spacing=1 时各方块紧贴）
    controller_scale: 层控制器尺寸缩放

    构建后总控 rubikMaster 上有统一的 rubikSpacing 可动画属性（默认 1）：
    一条连接同时驱动节点的 spacingX/Y/Z——调整它可整体放大/缩小方块间隔：
    骨骼与小控制器位置按间距重排，层控制器平面按轴向缩放跟随，
    蒙皮方块网格（polyCube 尺寸）、小控制器与总控曲线形状（几何层）
    同步缩放，骨骼自身 scale 不受 spacing 影响。需要单轴非均匀间距时
    在 Node Editor 断开对应 spacingX/Y/Z 手调（层控跟随节点值）。

    返回
    ----
    (总控控制器, 节点, 层控制器列表, 小方块列表)

    层级说明
    ----
    骨骼与方块是被节点输出驱动的"被动物"，放在场景顶层独立组里；
    控制器放在总控 rubikMaster 下。骨骼不能放进控制器层级：
    蒙皮后方块顶点由骨骼钉住，若骨骼再受控制器父级变换影响会产生
    双重变换；且顶层独立组在大纲里也更清晰。

    同一场景可重复构建多个魔方：本次所有对象自动追加后缀 _2、_3 ...
    （例如 rubikMaster_2 / rubikCubie_0_2 / rubikCtrl_X0_2），
    各魔方的节点、骨骼、控制器相互独立，互不影响。
    """
    dims = (max(1, int(dimX)), max(1, int(dimY)), max(1, int(dimZ)))
    dx, dy, dz = dims
    if max(dims) > MAX_DIM:
        raise ValueError("尺寸 %dx%dx%d 超过单轴最大层数 MAX_DIM=%d，"
                         "请在 rubikCube.py 中增大 MAX_DIM。" % (dx, dy, dz, MAX_DIM))

    cubie_n = dx * dy * dz
    slice_n = dx + dy + dz

    cmds.undoInfo(openChunk=True)
    try:
        # 名字后缀：同场景已存在魔方时自动加 _2、_3 ...，避免任何重名
        tag = _next_cube_tag()

        # 总控层级（两个 offset 组 + 总控制器曲线），全部在原点创建：
        #   rubikMaster_grp (顶层 offset 组 1，留给动画师整体对位)
        #   └── rubikMaster_ctrl_grp (offset 组 2)
        #       └── rubikMaster (总控制器曲线)
        #           ├── rubikCube_ctrls  (层控制器)
        #           └── rubikCube_cctrls (每个小方块的小控制器)
        master_grp = cmds.group(empty=True, name="rubikMaster_grp" + tag)
        master_ctrl_grp = cmds.group(empty=True, name="rubikMaster_ctrl_grp" + tag)
        cmds.parent(master_ctrl_grp, master_grp)
        master = _create_master_controller(dims, tag)
        cmds.parent(master, master_ctrl_grp)
        ctrls_grp = cmds.group(empty=True, name="rubikCube_ctrls" + tag, parent=master)
        cctrls_grp = cmds.group(empty=True, name="rubikCube_cctrls" + tag, parent=master)
        cubies_grp = cmds.group(empty=True, name="rubikCube_cubies" + tag)

        # 1. 创建小方块（索引顺序必须与 _home_position 一致），
        #    同时收集各外露面，稍后按方向统一上色
        cubies = []
        cubie_makers = []
        face_groups = dict((key, []) for key in FACE_COLORS)
        for i in range(cubie_n):
            x, y, z = _home_position(dims, i)
            ix, iy, iz = _home_indices(dims, i)
            cube, maker = cmds.polyCube(w=cubie_scale, h=cubie_scale,
                                        d=cubie_scale,
                                        name="rubikCubie_%d%s" % (i, tag))
            cmds.parent(cube, cubies_grp)
            cmds.setAttr(cube + ".translate", float(x), float(y), float(z))
            for key in _outer_faces(ix, iy, iz, dims):
                face_groups[key].append("%s.f[%d]" % (cube, FACE_INDEX[key]))
            cubies.append(cube)
            cubie_makers.append(maker)

        # 1.1 按方向统一上色（每个方向只需一次 assign，避免逐面操作）
        for key, faces in face_groups.items():
            if faces:
                cmds.sets(faces, e=True, forceElement=_get_shading_group(*FACE_COLORS[key]))

        # 2. 创建节点并设置尺寸
        node = cmds.createNode("rubikCubeNode", name="rubikCubeNode1" + tag)
        cmds.setAttr(node + ".dimX", dx)
        cmds.setAttr(node + ".dimY", dy)
        cmds.setAttr(node + ".dimZ", dz)

        # 2.1 总控 spacingX/Y/Z 属性：驱动骨骼排列位置与方块网格尺寸
        _setup_spacing(master, node, cubie_scale, cubie_makers, tag)

        # 2.2 总控"开启缓存"属性：connectAttr 连到节点 + 联动 Maya 缓存播放
        _setup_master_cache(master, node)

        # 2.3 总控曲线形状随间距缩放（几何层 transformGeometry，不占 scale）
        _setup_master_gizmo(master, node, dims, tag)

        # 3. 骨骼层级：先建一个位于原点的总骨骼，所有小方块骨骼子化到它下面，
        #    最后用总控制器 parentConstraint + scaleConstraint 约束总骨骼。
        #    这样移动/旋转/缩放总控制器即可整体操控魔方，而骨骼层级与控制器
        #    层级相互独立（约束而非父子，避免蒙皮双重变换）。
        root_joint = cmds.createNode("joint", name="rubikRootJoint" + tag)
        cmds.setAttr(root_joint + ".translate", 0, 0, 0)
        cmds.setAttr(root_joint + ".rotate", 0, 0, 0)
        cmds.setAttr(root_joint + ".radius", 0.6)

        # 先建每个小方块的骨骼并放到初始位置，再子化到总骨骼（总骨骼在原点、
        # 单位变换，子化前后子骨骼的局部位移不变）。
        # 骨骼与总骨骼之间插入旋转缓存骨骼 rubikJointRot_i，直连节点
        # outRotateCache[i]（连续 unwrap 的累计旋转，受总控"开启缓存"门控）：
        #   rubikRootJoint └ rubikJointRot_i(缓存骨骼) └ rubikJoint_i(蒙皮关节)
        # orientConstraint 两侧的累计旋转（缓存骨骼 vs 控制器 offset 组）互相
        # 抵消，关节本地只剩小控制器偏移，读数不再 ±180 翻转；关闭缓存时
        # 缓存骨骼归零，退回纯约束模式（组合仍正确，读数会翻转）。
        joints = []
        rot_joints = []
        for i in range(cubie_n):
            # 缓存骨骼：与蒙皮关节同样的骨骼继承，半径缩小以示区别
            rg = cmds.createNode("joint",
                                 name="rubikJointRot_%d%s" % (i, tag))
            cmds.parent(rg, root_joint)
            cmds.setAttr(rg + ".radius", 0.15)
            j = cmds.createNode("joint", name="rubikJoint_%d%s" % (i, tag))
            hx, hy, hz = _home_position(dims, i)
            cmds.setAttr(j + ".translate", float(hx), float(hy), float(hz))
            cmds.setAttr(j + ".radius", 0.3)
            cmds.parent(j, rg)
            cmds.connectAttr("%s.outRotateCache[%d]" % (node, i),
                             "%s.rotate" % rg)
            joints.append(j)
            rot_joints.append(rg)

        # 蒙皮：每个方块绑到自己的骨骼（单关节刚性绑定，100% 权重）
        for i in range(cubie_n):
            cmds.select(joints[i], cubies[i], ne=True)
            cmds.skinCluster(toSelectedBones=True,
                             name="rubikSkin_%d%s" % (i, tag),
                             bindMethod=0,
                             maximumInfluences=1,
                             dropoffRate=4.0,
                             removeUnusedInfluence=False)

        # 4. 每个小方块一套 FK 小控制器：
        #    rubikCCtrl_grp_i (offset 组，在 rubikCube_cctrls 下)
        #    └── rubikCCtrl_i  (小控制器，本地 TRS 供动画师逐块偏移)
        #    数据流：
        #      节点 out TR ──> offset 组 TR（跟随方块逻辑位置/姿态，
        #                     rotate = 已提交累积 ∘ 进行中 的层旋转）
        #      小控制器世界位置 ── pointConstraint ──> 骨骼本地平移
        #      小控制器世界旋转 ── orientConstraint ──> 骨骼本地旋转
        #      （骨骼父链上的 rubikJointRot_i 缓存骨骼抵消层旋转，关节
        #        本地读数 = 纯小控制器偏移，不再 ±180 翻转；见第 3 节）
        #      小控制器 scale ── scaleConstraint ──> 骨骼 scale
        #    平移/旋转都必须用约束跟随而非分量相加：offset 组带着层旋转
        #    姿态，小控制器的世界偏移/偏转已被层旋转过；欧拉分量相加把
        #    偏移留在世界轴上——平移表现为"层转 180 度后移出去的方块朝
        #    内"，旋转表现为"层转动后偏转方向错误"（实测先转小控制器
        #    rotateY=30 再转 X 层 90，骨骼世界旋转与小控制器差 45 度级
        #    的错位，矩阵点积 0.75）。约束在总骨骼空间解算，master 自身
        #    的变换在骨骼树与控制器层级两侧抵消，不会双倍。
        cctrls = []
        # gizmo 几何随间距缩放：源曲线（intermediate）经 transformGeometry
        # 按 spacing 缩放后喂给每个小控制器曲线的 .create。缩放在几何层
        # 完成，不占用小控制器的 scale 通道（用户缩放仍经 scaleConstraint
        # 驱动骨骼），也不会在约束目标上引入非均匀缩放切变；间距默认 1
        # 时矩阵为单位阵，gizmo 与旧版完全一致。
        giz_src = _create_cubie_controller("rubikGizmoSrc" + tag,
                                           cubie_scale * 1.25)
        cmds.parent(giz_src, cctrls_grp)
        giz_shp = cmds.listRelatives(giz_src, shapes=True, fullPath=True)[0]
        cmds.setAttr(giz_shp + ".intermediateObject", 1)
        giz_mat = cmds.createNode("composeMatrix", name="rubikGizmoScale" + tag)
        cmds.connectAttr(node + ".spacingX", giz_mat + ".inputScaleX")
        cmds.connectAttr(node + ".spacingY", giz_mat + ".inputScaleY")
        cmds.connectAttr(node + ".spacingZ", giz_mat + ".inputScaleZ")
        giz_tg = cmds.createNode("transformGeometry", name="rubikGizmoTg" + tag)
        cmds.connectAttr(giz_shp + ".local", giz_tg + ".inputGeometry")
        cmds.connectAttr(giz_mat + ".outputMatrix", giz_tg + ".transform")
        for i, j in enumerate(joints):
            cgrp = cmds.group(empty=True, name="rubikCCtrl_grp_%d%s" % (i, tag))
            # gizmo 边长 1.25 倍方块边长：六个面框（h=0.625）露在方块
            # 表面（半边长 0.5）之外，方便点选；几何经 rubikGizmoTg 按
            # 间距缩放，与方块（maker WHD 随间距）始终匹配
            ctrl = _create_cubie_controller("rubikCCtrl_%d%s" % (i, tag),
                                            cubie_scale * 1.25)
            ctrl_shp = cmds.listRelatives(ctrl, shapes=True, fullPath=True)[0]
            cmds.connectAttr(giz_tg + ".outputGeometry", ctrl_shp + ".create")
            cmds.parent(ctrl, cgrp)
            cmds.parent(cgrp, cctrls_grp)

            out_t = "%s.outTranslate[%d]" % (node, i)
            out_r = "%s.outRotate[%d]" % (node, i)
            # 节点输出复制一份给 offset 组
            cmds.connectAttr(out_t, "%s.translate" % cgrp)
            cmds.connectAttr(out_r, "%s.rotate" % cgrp)

            # 平移：pointConstraint 跟随小控制器世界位置（在总骨骼空间内
            # 即"槽位 + 已随层旋转的偏移"），偏移随层旋转。
            cmds.pointConstraint(ctrl, j,
                                 name="rubikCubiePosConstraint_%d%s" % (i, tag),
                                 maintainOffset=False)

            # 旋转：orientConstraint 跟随小控制器世界旋转
            # （= 层旋转 ∘ 小控制器本地偏转）。缓存开启时父链上的
            # rubikJointRot_i（outRotateCache）缓存骨骼抵消层旋转，关节本地
            # 只剩偏转本身，读数不翻转；关闭时退回纯约束组合模式。
            cmds.orientConstraint(ctrl, j,
                                  name="rubikCubieOrientConstraint_%d%s" % (i, tag),
                                  maintainOffset=False)

            # 小控制器缩放约束骨骼（初始都是单位缩放，无需 maintainOffset）。
            # 必须用 scaleConstraint 而非本地 scale 直连：子骨骼默认开启
            # segmentScaleCompensate，会抵消总骨骼继承来的缩放；scaleConstraint
            # 写入的是控制器世界缩放（含总控缩放），总控缩放时方块才会随之
            # 放大。直连本地 scale=1 时 compensate 会把方块留在原大小。
            cmds.scaleConstraint(ctrl, j,
                                 name="rubikCubieScaleConstraint_%d%s" % (i, tag),
                                 maintainOffset=False)
            cctrls.append(ctrl)

        # 总控制器约束总骨骼：平移/旋转用 parentConstraint，缩放用 scaleConstraint。
        # 两者此时都在原点、单位缩放，不需要 maintainOffset。
        cmds.parentConstraint(master, root_joint,
                              name="rubikRootParentConstraint" + tag,
                              maintainOffset=False)
        cmds.scaleConstraint(master, root_joint,
                             name="rubikRootScaleConstraint" + tag,
                             maintainOffset=False)

        # 5. 创建每个层的控制器并连接。每个层控制器外套一个 offset 组：
        #    rubikCtrl_grp_X0 (位置=切片坐标*间距；面内两轴按 层数*间距 缩放)
        #    └── rubikCtrl_X0 (控制器本体，只留旋转通道)
        #    这样调整总控 spacing 时，层控制器平面会随新魔方大小移动和
        #    非均匀缩放，而控制器自身的旋转/锁定通道完全不受影响。
        controllers = []
        # 每个层轴在控制器平面内的两根轴及其层数
        inplane = {0: (("Y", 1, dy), ("Z", 2, dz)),
                   1: (("X", 0, dx), ("Z", 2, dz)),
                   2: (("X", 0, dx), ("Y", 1, dy))}
        for k in range(slice_n):
            axis, coord = _slice_coord(k, dims)
            _, local_idx = _axis_local_index(k, dims)
            suffix = "%s%d%s" % ("XYZ"[axis], local_idx, tag)
            name = _controller_name(axis, k, dims, tag)

            sgrp = cmds.group(empty=True, name="rubikCtrl_grp_%s" % suffix,
                              parent=ctrls_grp)
            ctrl = _create_slice_controller(axis, controller_scale, name)
            cmds.parent(ctrl, sgrp)
            controllers.append(ctrl)

            # 位置：coord * node.spacing<axis> -> sgrp 对应平移通道
            # （从节点取值：与方块/位置同源，节点级非均匀手调时层控也跟随）
            ch_axis = "XYZ"[axis]
            pos_mult = cmds.createNode(
                "multiplyDivide", name="rubikSlicePos_%s" % suffix)
            cmds.setAttr(pos_mult + ".operation", 1)
            cmds.setAttr(pos_mult + ".input1%s" % ch_axis, float(coord))
            cmds.connectAttr("%s.spacing%s" % (node, ch_axis),
                             pos_mult + ".input2%s" % ch_axis)
            cmds.connectAttr(pos_mult + ".output%s" % ch_axis,
                             "%s.translate%s" % (sgrp, ch_axis))

            # 面内两轴尺寸：层数 * node.spacing -> sgrp 对应缩放通道
            size_mult = cmds.createNode(
                "multiplyDivide", name="rubikSliceScale_%s" % suffix)
            cmds.setAttr(size_mult + ".operation", 1)
            for ch, sp_axis, dim_n in inplane[axis]:
                cmds.setAttr(size_mult + ".input1%s" % ch, float(dim_n))
                cmds.connectAttr("%s.spacing%s" % (node, ch),
                                 size_mult + ".input2%s" % ch)
                cmds.connectAttr(size_mult + ".output%s" % ch,
                                 "%s.scale%s" % (sgrp, ch))

            channel = {0: "rotateX", 1: "rotateY", 2: "rotateZ"}[axis]
            input_name = "rot%s%d" % ("XYZ"[axis], local_idx)
            cmds.connectAttr("%s.%s" % (ctrl, channel), "%s.%s" % (node, input_name), f=True)
            cmds.connectAttr("%s.outVisible[%d]" % (node, k), "%s.visibility" % ctrl, f=True)

            # 全部连接完成后再上色并收敛通道（放在最后，避免锁定影响前面的连接）
            _setup_slice_controller(ctrl, axis)

        cmds.select(clear=True)
        print(u"魔方绑定构建完成：master=%s 节点=%s 尺寸=%dx%dx%d 层控制器=%d"
              % (master, node, dx, dy, dz, slice_n))
        return master, node, controllers, cubies
    finally:
        cmds.undoInfo(closeChunk=True)


# ---------------------------------------------------------------------------
# 构建命令（让用户可以直接 cmds.rubikCube() 构建）
# ---------------------------------------------------------------------------
class RubikCubeCommand(ompx.MPxCommand):
    kCommandName = "rubikCube"

    def __init__(self):
        ompx.MPxCommand.__init__(self)

    @staticmethod
    def syntaxCreator():
        syntax = om.MSyntax()
        syntax.addFlag("-dx", "-dimX", om.MSyntax.kLong)
        syntax.addFlag("-dy", "-dimY", om.MSyntax.kLong)
        syntax.addFlag("-dz", "-dimZ", om.MSyntax.kLong)
        syntax.addFlag("-cs", "-cubieScale", om.MSyntax.kDouble)
        syntax.addFlag("-cts", "-controllerScale", om.MSyntax.kDouble)
        syntax.addFlag("-rst", "-reset", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        try:
            db = om.MArgDatabase(self.syntax(), args)
        except Exception:
            db = None

        # -reset：一键还原所有魔方到初始状态
        if db is not None and db.isFlagSet("-reset"):
            nodes = cmds.ls(type="rubikCubeNode") or []
            for nd in nodes:
                resetCube(nd)
            self.setResult(nodes)
            return

        dx, dy, dz = 3, 3, 3
        cubie_scale = 1.0
        controller_scale = 1.0
        if db is not None:
            if db.isFlagSet("-dimX"):
                dx = db.flagArgumentInt("-dimX", 0)
            if db.isFlagSet("-dimY"):
                dy = db.flagArgumentInt("-dimY", 0)
            if db.isFlagSet("-dimZ"):
                dz = db.flagArgumentInt("-dimZ", 0)
            if db.isFlagSet("-cubieScale"):
                cubie_scale = db.flagArgumentDouble("-cubieScale", 0)
            if db.isFlagSet("-controllerScale"):
                controller_scale = db.flagArgumentDouble("-controllerScale", 0)
        master, node, controllers, cubies = buildRubikCube(
            dx, dy, dz, cubie_scale, controller_scale)
        self.setResult(master)

    @classmethod
    def cmdCreator(cls):
        return ompx.asMPxPtr(RubikCubeCommand())


# ---------------------------------------------------------------------------
# 插件注册
# ---------------------------------------------------------------------------
def initializePlugin(mobject):
    # 版本 2.0.0：非兼容性升级（骨骼层级/约束结构变更、总控间距属性合并），
    # 旧场景需重新构建魔方
    plugin = ompx.MFnPlugin(mobject, "KangmingZhan", "2.0.0")

    # 获取插件所在目录的真实路径。
    # MFnPlugin.loadPath() 在某些 Maya 版本/平台下返回的不是 .py 文件的
    # 完整路径（可能少了末尾目录层级），用 __file__ 更可靠。
    import os
    try:
        plugin_dir = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    except Exception:
        plugin_dir = os.path.dirname(plugin.loadPath()).replace("\\", "/")
    if plugin_dir not in sys.path:
        sys.path.append(plugin_dir)

    try:
        plugin.registerNode(
            RubikCubeNode.kNodeName,
            RubikCubeNode.kNodeId,
            RubikCubeNode.nodeCreator,
            RubikCubeNode.nodeInitializer,
        )
        print(u"成功注册节点: %s" % RubikCubeNode.kNodeName)
    except Exception as e:
        sys.stderr.write(u"注册节点失败: %s, 错误: %s\n" % (RubikCubeNode.kNodeName, e))

    try:
        plugin.registerCommand(
            RubikCubeCommand.kCommandName,
            RubikCubeCommand.cmdCreator,
            RubikCubeCommand.syntaxCreator,
        )
        print(u"成功注册命令: %s" % RubikCubeCommand.kCommandName)
    except Exception as e:
        sys.stderr.write(u"注册命令失败: %s, 错误: %s\n" % (RubikCubeCommand.kCommandName, e))

    # 注册 Attribute Editor 模板：选中节点时 AE 顶部出现"还原魔方"按钮。
    # 参考 get_from_array_.py 的 MEL 字符串方式：Maya 自动查找
    # AE<节点类型名>Template 这个 MEL proc 来构建 AE 界面。
    # 传入插件目录，嵌入按钮 command，确保点击时 sys.path 包含该目录。
    createAETemplate(plugin_dir)

    # 场景打开后重建总控缓存回调（MNodeMessage 回调不随场景保存持久化）。
    # 回调 id 存入模块列表保引用；插件卸载时 Maya 自动移除该回调。
    _SCENE_OPEN_CB.append(
        om.MSceneMessage.addCallback(om.MSceneMessage.kAfterOpen,
                                     _on_scene_opened, None))


def createAETemplate(plugin_dir=""):
    """注册 rubikCubeNode 的 Attribute Editor 模板。

    在 AE 顶部加一个"还原魔方"按钮，点击调用 rubikCube.resetCube()。
    模板用 MEL proc 注册（AE<节点类型名>Template），按钮 command 通过
    python() 调用 rubikCube 模块的 resetCube。plugin_dir 嵌入到 command
    里，确保按钮点击时 sys.path 包含插件目录，避免 import 失败。
    """
    # 用 cmds.pluginInfo 查真实路径，比 MFnPlugin.loadPath() 和 __file__ 可靠
    import os as _os
    try:
        real_path = cmds.pluginInfo("rubikCube", q=True, path=True)
        plugin_dir = _os.path.dirname(real_path).replace("\\", "/")
    except Exception:
        pass
    safe_dir = plugin_dir.replace("\\", "/")
    # py2 下 pluginInfo/__file__ 可能返回 utf-8 字节串，统一转 unicode，
    # 避免与 unicode 模板做 replace 时触发 ascii 解码
    if not isinstance(safe_dir, type(u"")):
        safe_dir = safe_dir.decode("utf-8")
    # 模板用 u 前缀：内部含中文，且要与 pluginInfo 返回的 unicode 路径 replace。
    # py2 下 mel.eval 接受 unicode（内部转 UTF-8），py3 原生 str。
    template = u'''
global proc AErubikCubeNodeTemplate(string $nodeName) {
    editorTemplate -beginScrollLayout;
    editorTemplate -beginLayout "操作 (Operate)" -collapse 0;
        editorTemplate -callCustom "AECreateResetButton" "AEUpdateResetButton" "nodeName" $nodeName;
    editorTemplate -endLayout;
    editorTemplate -beginLayout "基础属性 (Base Attributes)" -collapse 0;
        editorTemplate -addControl "dimX";
        editorTemplate -addControl "dimY";
        editorTemplate -addControl "dimZ";
    editorTemplate -endLayout;
    editorTemplate -beginLayout "选项 (Options)" -collapse 0;
        editorTemplate -addControl "unwrapEuler";
    editorTemplate -endLayout;
    editorTemplate -addExtraControls;
    editorTemplate -endScrollLayout;
}

// 清理节点名：$nodeName 可能带 .attr 后缀，取点之前的部分
global proc string rubikCleanNodeName(string $fullName) {
    string $parts[];
    tokenize $fullName "." $parts;
    return $parts[0];
}

global proc AECreateResetButton(string $attrName, string $nodeName) {
    string $cleanName = rubikCleanNodeName($nodeName);
    string $pluginDir = "__PLUGIN_DIR__";
    string $cmd = "import sys; d='" + $pluginDir + "'; d not in sys.path and sys.path.append(d); import rubikCube; rubikCube.resetCube('" + $cleanName + "')";
    columnLayout -adjustableColumn true -rowSpacing 4;

    // ---- 使用说明（只读文本，随操作栏一起显示，让用户直接看到）----
    scrollField -editable false -wordWrap true -height 150
        -text ("使用注意 (Notes)：\\n"
             + "1. K 动画时：同一时间只允许一个轴向的层控制器处于非 90 度整数倍的"
             + "旋转中，同时转多个轴会造成骨骼数值错乱。\\n"
             + "2. 总控「开启缓存」打开时，关节的累计旋转会自动叠加到缓存骨骼"
             + "(rubikJointRot_*) 上，关节本地只保留小控制器偏移量。\\n"
             + "3. 缓存或骨骼数据出错时，点下方「还原魔方」：自动重置节点内部状态，"
             + "并把所有层控制器与骨骼数值清理归零。\\n"
             + "4. 快速构建 (Quick Build)：cmds.rubikCube(dimX=3, dimY=3, dimZ=3)")
        rubikHelpNotes;
    separator -height 6 -style "none";

    button -label "还原魔方 (Reset Cube)"
           -command ("python(\\"" + $cmd + "\\")")
           -annotation "重置 cubeState + 归零所有层控制器与骨骼缓存 (Reset cubeState + zero controllers/bones)"
           -height 28
           btnResetCube;
    separator -height 8 -style "in";

    // ---- GitHub 项目地址：可选中复制；被修改时 changeCommand 自动改回 ----
    text -label "项目地址 (GitHub)：内容可复制，不可修改" -align "left";
    textField -text "__GITHUB_URL__"
              -changeCommand "AERevertGithubLink()"
              -annotation "项目主页，可复制；修改会被自动还原"
              githubLinkField;
    setParent ..;
}

// GitHub 链接防改：任何编辑都会被还原为固定地址
global proc AERevertGithubLink() {
    if (`textField -exists githubLinkField`) {
        textField -edit -text "__GITHUB_URL__" githubLinkField;
    }
}

global proc AEUpdateResetButton(string $attrName, string $nodeName) {
    string $cleanName = rubikCleanNodeName($nodeName);
    string $pluginDir = "__PLUGIN_DIR__";
    string $cmd = "import sys; d='" + $pluginDir + "'; d not in sys.path and sys.path.append(d); import rubikCube; rubikCube.resetCube('" + $cleanName + "')";
    if (`button -exists btnResetCube`) {
        button -edit -command ("python(\\"" + $cmd + "\\")") btnResetCube;
    }
    // AE 刷新时同步还原链接文本（双保险）
    AERevertGithubLink();
}
'''
    template = template.replace("__PLUGIN_DIR__", safe_dir)
    template = template.replace("__GITHUB_URL__",
                                "https://github.com/MengXingXiaoMing/maya-rubikcube-node")
    # py2 下 MEL 源码解析器在 Windows 按系统 ANSI 代码页（中文版为 GBK/936）
    # 读取字节；直接喂 UTF-8 会被当 GBK 误读，且双字节解析会吞掉中文字符串
    # 后面的引号报 "Unterminated string"。因此按 mbcs(系统ANSI) 编码；
    # 非 Windows 无 mbcs 编解码器时回退 UTF-8（Mac/Linux 的 MEL 通常按 UTF-8）。
    # py3 下 mel.eval 直接接受 str。
    mel_src = template
    if sys.version_info[0] < 3 and isinstance(mel_src, type(u"")):
        try:
            mel_src = mel_src.encode("mbcs")
        except (UnicodeError, LookupError):
            mel_src = mel_src.encode("utf-8")
    mel.eval(mel_src)


def uninitializePlugin(mobject):
    plugin = ompx.MFnPlugin(mobject)
    # 清空 undo 队列，避免卸载时报 "still in use"
    try:
        cmds.flushUndo()
    except Exception:
        pass
    plugin.deregisterNode(RubikCubeNode.kNodeId)
    plugin.deregisterCommand(RubikCubeCommand.kCommandName)
