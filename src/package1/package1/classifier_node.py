# ============================================================
# NODO ROS2 — CLASIFICADOR DE DESHECHOS CON YOLO11 NCNN
# Suscribe a: camera/image_raw  (sensor_msgs/Image)
# Publica en: waste/classification        (std_msgs/String)
#             waste/classification_detail (std_msgs/String, JSON)
#             /trajectory_set             (std_msgs/Int32)
# ============================================================

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String, Int32
from cv_bridge import CvBridge

import cv2
import numpy as np
import json
import time
from ultralytics import YOLO


# ============================================================
# CONFIGURACIÓN
# ============================================================

MODELO_PATH      = '/home/camilo/waste_model/best_ncnn_model'
IMG_SIZE         = 256
UMBRAL_CONFIANZA = 0.60
CLASIFICAR_CADA  = 3
TIEMPO_BLOQUEO   = 4.0  # segundos

# ============================================================
# MAPEO DE CLASES → TRAYECTORIAS
# ============================================================

CLASE_A_SET: dict[str, int] = {
    'aprovechable'    : 1,
    'no_aprovechable': 2,
    'organico'       : 3,
}

# 'vacio' NO se incluye:
# se clasifica normalmente pero no mueve el robot.


# ============================================================
# NODO
# ============================================================

class ClassifierNode(Node):

    def __init__(self):
        super().__init__('classifier_node')

        # ── Cargar modelo YOLO NCNN ───────────────────────────
        self.get_logger().info(
            f'Cargando modelo desde {MODELO_PATH}...'
        )

        # task='classify' obligatorio para NCNN classify
        self.model = YOLO(MODELO_PATH, task='classify')

        # ── Warmup ────────────────────────────────────────────
        dummy = np.zeros((IMG_SIZE, IMG_SIZE, 3), dtype=np.uint8)

        self.model.predict(
            source=dummy,
            imgsz=IMG_SIZE,
            verbose=False,
            augment=False
        )

        self.get_logger().info(
            'Modelo cargado y warmup completado.'
        )

        self.get_logger().info(
            f'Clases detectadas: {self.model.names}'
        )

        # ── Variables internas ────────────────────────────────
        self.bridge        = CvBridge()
        self.counter       = 0
        self.tiempo_hasta  = 0.0

        # ── Suscripción cámara ────────────────────────────────
        self.subscription = self.create_subscription(
            Image,
            'camera/image_raw',
            self.imagen_callback,
            10
        )

        # ── Publicadores ──────────────────────────────────────
        self.pub_clase = self.create_publisher(
            String,
            'waste/classification',
            10
        )

        self.pub_detalle = self.create_publisher(
            String,
            'waste/classification_detail',
            10
        )

        self.pub_trayectoria = self.create_publisher(
            Int32,
            '/trajectory_set',
            10
        )

        self.get_logger().info(
            'Classifier node listo.\n'
            '  Suscrito a   : camera/image_raw\n'
            '  Publicando en: waste/classification\n'
            '  Publicando en: waste/classification_detail\n'
            '  Publicando en: /trajectory_set\n'
            f'  Mapeo clases : {CLASE_A_SET}\n'
            f'  Bloqueo post-movimiento: {TIEMPO_BLOQUEO}s'
        )

    # ============================================================
    # CALLBACK
    # ============================================================

    def imagen_callback(self, msg: Image):

        self.counter += 1

        # ── Bloqueo temporal mientras el robot se mueve ───────
        if time.time() < self.tiempo_hasta:
            return

        # ── Saltar frames ─────────────────────────────────────
        if self.counter % CLASIFICAR_CADA != 0:
            return

        try:

            # ── ROS Image → OpenCV ────────────────────────────
            frame = self.bridge.imgmsg_to_cv2(
                msg,
                desired_encoding='bgr8'
            )

            # ── Inferencia ────────────────────────────────────
            t_ini = time.time()

            results = self.model.predict(
                source=frame,
                imgsz=IMG_SIZE,
                verbose=False,
                augment=False,
            )

            t_ms = (time.time() - t_ini) * 1000

            # ── Extraer clasificación ─────────────────────────
            probs     = results[0].probs
            clase_idx = int(probs.top1)
            confianza = float(probs.top1conf)

            clase = self.model.names[clase_idx]

            todas = probs.data.tolist()

            # ── Aplicar umbral ────────────────────────────────
            clase_publicada = (
                clase
                if confianza >= UMBRAL_CONFIANZA
                else 'inseguro'
            )

            # ── Publicar clase ────────────────────────────────
            msg_clase = String()
            msg_clase.data = clase_publicada

            self.pub_clase.publish(msg_clase)

            # ── Publicar JSON detalle ─────────────────────────
            detalle = {
                'clase': clase_publicada,

                'confianza': round(confianza, 4),

                'probabilidades': {
                    self.model.names[i]: round(float(p), 4)
                    for i, p in enumerate(todas)
                },

                'tiempo_ms': round(t_ms, 1),

                'frame_numero': self.counter,
            }

            msg_detalle = String()
            msg_detalle.data = json.dumps(
                detalle,
                ensure_ascii=False
            )

            self.pub_detalle.publish(msg_detalle)

            # ====================================================
            # PUBLICAR TRAYECTORIA
            # ====================================================

            if (
                clase_publicada != 'inseguro'
                and clase_publicada in CLASE_A_SET
            ):

                set_id = CLASE_A_SET[clase_publicada]

                msg_tray = Int32()
                msg_tray.data = set_id

                self.pub_trayectoria.publish(msg_tray)

                # ── Activar bloqueo temporal ──────────────────
                self.tiempo_hasta = (
                    time.time() + TIEMPO_BLOQUEO
                )

                self.get_logger().info(
                    f'[Frame {self.counter}]  '
                    f'→ /trajectory_set: {set_id}  '
                    f'(clase: {clase_publicada})  '
                    f'| bloqueo {TIEMPO_BLOQUEO}s'
                )

            elif clase_publicada == 'inseguro':

                self.get_logger().debug(
                    f'[Frame {self.counter}]  '
                    f'Clasificación insegura '
                    f'({confianza*100:.1f}%)'
                )

            elif clase_publicada == 'vacio':

                self.get_logger().info(
                    f'[Frame {self.counter}]  '
                    f'Escena vacía — sin movimiento.'
                )

            else:

                self.get_logger().warn(
                    f'[Frame {self.counter}]  '
                    f'Clase "{clase_publicada}" '
                    f'no está en CLASE_A_SET.'
                )

            # ── Log general ────────────────────────────────────
            self.get_logger().info(
                f'[Frame {self.counter}]  '
                f'{clase_publicada.upper():<20}  '
                f'confianza: {confianza*100:.1f}%  '
                f'({t_ms:.0f} ms)'
            )

        except Exception as e:

            self.get_logger().error(
                f'Error en clasificación: {str(e)}'
            )


# ============================================================
# MAIN
# ============================================================

def main(args=None):

    rclpy.init(args=args)

    node = ClassifierNode()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()