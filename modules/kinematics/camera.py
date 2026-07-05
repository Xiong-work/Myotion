# import standard library

# import third party library
from numpy.linalg import inv

# import local library
from modules.kinematics.matrix import Matrix
from modules.kinematics.object3d import Object3D


class Camera(Object3D):
    def __init__(self, angleOfView=60, aspectRatio=1, near=0.1, far=1000, center=[0, 0, 0]):
        super().__init__()
        self.focus = center
        self.angleOfView = angleOfView
        self.aspectRatio = aspectRatio
        self.near = near
        self.far = far
        self.projectionMatrix = Matrix.makePerspective(
            angleOfView, aspectRatio, near, far
        )
        self.viewMatrix = Matrix.makeIdentity()

    def updateViewMatrix(self):
        self.viewMatrix = inv(self.getWorldMatrix())

    def setPerspective(self, angleOfView=50, aspectRatio=1, near=0.1, far=1000):
        self.angleOfView = angleOfView
        self.aspectRatio = aspectRatio
        self.near = near
        self.far = far
        self.projectionMatrix = Matrix.makePerspective(
            angleOfView, aspectRatio, near, far
        )

    def setAspectRatio(self, aspectRatio):
        """Recompute the projection matrix for a new aspect ratio only,
        keeping the current field of view/near/far -- call this from
        resizeGL so the rendered scene doesn't stretch/squash when the
        viewport is resized non-uniformly (see Base.resizeGL)."""
        self.setPerspective(self.angleOfView, aspectRatio, self.near, self.far)

    def setOrthographic(self, left=-1, right=1, bottom=-1, top=1, near=-1, far=1):
        self.projectionMatrix = Matrix.makeOrthographic(
            left, right, bottom, top, near, far
        )

    def rotateY(self, angle):
        self.translate(angle*500,0,0)
        self.lookAt(self.focus)
    
    def rotateX(self, angle):
        self.translate(0,-angle*500,0)
        self.lookAt(self.focus)

    def pan(self, dx, dy):
        """Translate the camera within its own screen plane (local X/Y),
        keeping self.focus moving with it. rotateX/rotateY re-aim the camera
        at self.focus every time (see above), so if focus stayed fixed at
        the original center, the next orbit nudge after a pan would snap the
        view back toward that original point, undoing the pan."""
        right, up = self.getRotationMatrix()[:, 0], self.getRotationMatrix()[:, 1]
        worldDelta = right * dx + up * dy
        self.translate(dx, dy, 0)
        self.focus = [self.focus[0] + worldDelta[0], self.focus[1] + worldDelta[1], self.focus[2] + worldDelta[2]]