"""
Nodo ROS2 - Firebase Uploader (Sensores)
Se suscribe al topic esp32/sensors (Vector3) y sube los datos
de las 3 canecas a Firebase Firestore en la colección 'residuos'.
"""

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Vector3
from rclpy.qos import QoSProfile

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from datetime import datetime, timezone
import threading
import time


# ============================================================
# CONFIGURACIÓN
# ============================================================
RUTA_CREDENCIALES = "/home/camilo/serviceAccountKey.json"
COLECCION_PRINCIPAL = "residuos"
INTERVALO_SUBIDA_SEG = 1.0


# ============================================================
# NODO ROS2
# ============================================================
class FirebaseUploaderNode(Node):
    def __init__(self):
        super().__init__('firebase_uploader_node')

        # --- Inicializar Firebase ---
        try:
            cred = credentials.Certificate(RUTA_CREDENCIALES)
            firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            self.get_logger().info('[Firebase] Conexión inicializada correctamente.')
        except Exception as e:
            self.get_logger().error(f'[Firebase] Error al inicializar: {e}')
            raise

        # --- Dato en memoria y control de frecuencia ---
        self._dato_sensores: dict | None = None
        self._lock = threading.Lock()
        self._ultimo_envio: float = 0.0

        # --- Subscriber a esp32/sensors ---
        qos = QoSProfile(depth=10)
        self.sub_sensors = self.create_subscription(
            Vector3,
            'esp32/sensors',
            self.sensors_callback,
            qos
        )
        self.get_logger().info('[ROS2] Suscrito a esp32/sensors.')

        # --- Timer que intenta subir a Firebase cada segundo ---
        self.timer = self.create_timer(1.0, self.timer_callback)

        self.get_logger().info(
            f'[Sistema] Nodo listo. Subiendo a Firebase cada {INTERVALO_SUBIDA_SEG}s.'
        )

    # ============================================================
    # CALLBACK esp32/sensors
    # ============================================================
    def sensors_callback(self, msg: Vector3):
        """
        Recibe el Vector3 publicado por ESP32SensorsNode.
            msg.x = s1  →  caneca_1_porcentaje
            msg.y = s2  →  caneca_2_porcentaje
            msg.z = s3  →  caneca_3_porcentaje
        Solo actualiza el dato en memoria; la subida la hace el timer.
        """
        try:
            dato = {
                "caneca_1_porcentaje": round(msg.x, 4),
                "caneca_2_porcentaje": round(msg.y, 4),
                "caneca_3_porcentaje": round(msg.z, 4),
            }

            with self._lock:
                self._dato_sensores = dato

            self.get_logger().debug(
                f'[Sensores] C1={msg.x:.2f}% | C2={msg.y:.2f}% | C3={msg.z:.2f}%'
            )

        except Exception as e:
            self.get_logger().error(f'[Sensors] Error procesando mensaje: {e}')

    # ============================================================
    # TIMER CALLBACK
    # ============================================================
    def timer_callback(self):
        ahora = time.time()

        if ahora - self._ultimo_envio < INTERVALO_SUBIDA_SEG:
            return

        with self._lock:
            dato = self._dato_sensores

        if dato is None:
            self.get_logger().warn(
                '[Firebase] Sin datos aún — esperando esp32/sensors.'
            )
            return

        dato_con_timestamp = {
            **dato,
            "timestamp":           datetime.now(timezone.utc).isoformat(),
            "timestamp_firestore": SERVER_TIMESTAMP,
        }

        self._guardar_lectura(dato_con_timestamp)
        self._ultimo_envio = ahora

    # ============================================================
    # ESCRITURA EN FIRESTORE
    # ============================================================
    def _guardar_lectura(self, datos: dict):
        """
        1. residuos/lecturas/historial/{auto-id}  → registro histórico
        2. residuos/estado_actual                 → estado en tiempo real
        """
        try:
            self.db.collection(COLECCION_PRINCIPAL) \
                   .document("lecturas") \
                   .collection("historial") \
                   .add(datos)

            self.db.collection(COLECCION_PRINCIPAL) \
                   .document("estado_actual") \
                   .set(datos)

            self.get_logger().info(
                f'[Firebase] Guardado — '
                f'C1={datos["caneca_1_porcentaje"]}% | '
                f'C2={datos["caneca_2_porcentaje"]}% | '
                f'C3={datos["caneca_3_porcentaje"]}%'
            )

        except Exception as e:
            self.get_logger().error(f'[Firebase] Error al guardar: {e}')


# ============================================================
# MAIN
# ============================================================
def main(args=None):
    rclpy.init(args=args)
    node = FirebaseUploaderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()