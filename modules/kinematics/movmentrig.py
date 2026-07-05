from PySide6.QtCore import Qt

from .object3d import Object3D


class MovementRig:
    def __init__(self, unitsPerSecond=1, degreesPerSecond=60):

        # initilize base Object3d; controls movement
        # and turn left/right
        super().__init__()

        # initialize attached Object3d; controls look up/down
        self.camera = Object3D()

        # control rate of movement
        self.unitsPerSecond = unitsPerSecond
        self.degreesPerSecond = degreesPerSecond

        # customizable key mappings
        # defaults: WASDRF (move), QE (turn), TG (look)
        self.KEY_MOVE_FORWARDS = Qt.Key_W
        self.KEY_MOVE_BACKWARDS = Qt.Key_S
        self.KEY_MOVE_LEFT = Qt.Key_A
        self.KEY_MOVE_RIGHT = Qt.Key_D
        self.KEY_MOVE_UP = Qt.Key_R
        self.KEY_MOVE_DOWN = Qt.Key_F
        self.KEY_TURN_LEFT = Qt.Key_Q
        self.KEY_TURN_RIGHT = Qt.Key_E
        self.KEY_LOOK_UP = Qt.Key_T
        self.KEY_LOOK_DOWN = Qt.Key_G

    # adding and removing objects applies to look attachment;
    # override funtions from Object3d class
    def add(self, camera):
        self.camera = camera

    def update(self, inputObejct, deltaTime):
        moveAmount = self.unitsPerSecond * deltaTime * 10
        rotateAmount = self.degreesPerSecond * 3.1415926 / 180.0 * deltaTime

        if inputObejct.isKeyPressed(self.KEY_MOVE_FORWARDS):
            self.camera.translate(0, 0, -moveAmount)
        if inputObejct.isKeyPressed(self.KEY_MOVE_BACKWARDS):
            self.camera.translate(0, 0, moveAmount)
        if inputObejct.isKeyPressed(self.KEY_MOVE_LEFT):
            self.camera.translate(-moveAmount, 0, 0)
        if inputObejct.isKeyPressed(self.KEY_MOVE_RIGHT):
            self.camera.translate(moveAmount, 0, 0)
        if inputObejct.isKeyPressed(self.KEY_MOVE_UP):
            self.camera.translate(0, moveAmount, 0)
        if inputObejct.isKeyPressed(self.KEY_MOVE_DOWN):
            self.camera.translate(0, -moveAmount, 0)
        if inputObejct.isKeyPressed(self.KEY_TURN_RIGHT):
            self.camera.rotateY(-rotateAmount)
        if inputObejct.isKeyPressed(self.KEY_TURN_LEFT):
            self.camera.rotateY(rotateAmount)
        if inputObejct.isKeyPressed(self.KEY_LOOK_UP):
            self.camera.rotateX(rotateAmount)
        if inputObejct.isKeyPressed(self.KEY_LOOK_DOWN):
            self.camera.rotateX(-rotateAmount)

        if inputObejct.isMouseDown(Qt.MouseButton.LeftButton):
            x, y = inputObejct.mouseMovement()
            self.camera.rotateY(x * rotateAmount)
            self.camera.rotateX(y * rotateAmount)

        # Hold the middle mouse button and drag to pan the view in its own
        # screen plane (translate up/down/left-right without rotating) --
        # the standard "middle-drag to pan" convention, complementing
        # left-drag-to-orbit and wheel-to-dolly.
        if inputObejct.isMouseDown(Qt.MouseButton.MiddleButton):
            x, y = inputObejct.middleMovement()
            panAmount = self.unitsPerSecond * 0.5
            self.camera.pan(x * panAmount, -y * panAmount)

        if inputObejct.wheelMovement != 0:
            self.camera.translate(0, 0, inputObejct.wheelMovement)
            inputObejct.wheelMovement = 0
