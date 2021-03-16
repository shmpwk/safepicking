import numpy as np

from . import transformations as tf


class Coordinate:
    def __init__(self, position=None, quaternion=None):
        self._position = np.zeros((3,), dtype=float)
        self._quaternion = np.array([0, 0, 0, 1], dtype=float)

        if position is not None:
            self.position = position
        if quaternion is not None:
            self.quaternion = quaternion

    def copy(self):
        return Coordinate(
            position=self.position.copy(), quaternion=self.quaternion.copy()
        )

    def __repr__(self):
        with np.printoptions(precision=3, suppress=True):
            position = np.array2string(self.position, separator=", ")
            euler = np.array2string(self.euler, separator=", ")
            quaternion = np.array2string(self.quaternion, separator=", ")
            return (
                f"{self.__class__.__name__}("
                f"\n  position={position},"
                f"\n  euler={euler},"
                f"\n  quaternion={quaternion},"
                f"\n)"
            )

    def translate(self, translation, wrt="local"):
        self.transform(tf.translation_matrix(translation), wrt=wrt)

    def rotate(self, euler, wrt="local"):
        self.rotation_transform(tf.euler_matrix(euler), wrt=wrt)

    def rotation_transform(self, rotation_transform, wrt="local"):
        rotation_transform = rotation_transform[:3, :3]
        rotation_matrix = self.matrix[:3, :3]
        if wrt == "local":
            rotation_matrix = rotation_matrix @ rotation_transform
        else:
            assert wrt == "world"
            rotation_matrix = rotation_transform @ rotation_matrix
        matrix = tf.translation_matrix(self.position)
        matrix[:3, :3] = rotation_matrix
        self._position = tf.translation_from_matrix(matrix)
        self._quaternion = tf.quaternion_from_matrix(matrix)

    def transform(self, transform, wrt="local"):
        if wrt == "local":
            matrix = self.matrix @ transform
        else:
            assert wrt == "world"
            matrix = transform @ self.matrix
        self._position = tf.translation_from_matrix(matrix)
        self._quaternion = tf.quaternion_from_matrix(matrix)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, position):
        self._position = np.asarray(position)

    @property
    def quaternion(self):
        return self._quaternion

    @quaternion.setter
    def quaternion(self, quaternion):
        self._quaternion = np.asarray(quaternion)

    @property
    def matrix(self):
        return tf.transformation_matrix(self._position, self._quaternion)

    @property
    def euler(self):
        return tf.euler_from_quaternion(self._quaternion)

    @property
    def pose(self):
        return self.position, self.quaternion
