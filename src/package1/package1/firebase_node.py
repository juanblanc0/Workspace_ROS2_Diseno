"""
Nodo ROS2 - Firebase Uploader
Se suscribe al topic esp32/state (JointState) y sube los datos
de velocidad y posición de los motores a Firebase Firestore.
No abre el puerto serial directamente — lee del topic que ya
publica ESP32SerialNode, evitando cualquier conflicto de puerto.
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
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
COLECCION_PRINCIPAL = "motores"

# Intervalo mínimo entre subidas a Firebase (segundos)
# El topic puede publicar muy rápido; esto evita saturar Firestore
INTERVALO_SUBIDA_SEG = 0.5


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

        # --- Control de frecuencia de subida ---
        # Guardamos el último dato recibido y un timestamp del último envío.
        # Así el subscriber actualiza el dato en memoria constantemente,
        # y un timer separado lo sube a Firebase cada INTERVALO_SUBIDA_SEG.
        self._ultimo_dato: dict | None = None
        self._lock = threading.Lock()  # evita condiciones de carrera entre hilos
        self._ultimo_envio: float = 0.0

        # --- Subscriber al topic esp32/state ---
        qos = QoSProfile(depth=10)
        self.subscription = self.create_subscription(
            JointState,
            'esp32/state',
            self.state_callback,
            qos
        )
        self.get_logger().info('[ROS2] Suscrito a esp32/state.')

        # --- Timer que intenta subir a Firebase cada segundo ---
        # Revisa si ya pasó INTERVALO_SUBIDA_SEG desde el último envío.
        # Usar un timer ROS2 es mejor que time.sleep() porque no bloquea el nodo.
        self.timer = self.create_timer(1.0, self.timer_callback)

        self.get_logger().info(
            f'[Sistema] Nodo listo. Subiendo a Firebase cada {INTERVALO_SUBIDA_SEG}s.'
        )

    # ============================================================
    # CALLBACK DEL SUBSCRIBER
    # Se ejecuta cada vez que ESP32SerialNode publica en esp32/state.
    # Solo actualiza el dato en memoria, no sube a Firebase aquí.
    # ============================================================
    def state_callback(self, msg: JointState):
        """
        Recibe el JointState publicado por ESP32SerialNode.
        Estructura del mensaje:
            msg.name       = ['motor1', 'motor2']
            msg.velocity   = [V1, V2]
            msg.position   = [P1, P2]
        """
        try:
            v1 = msg.velocity[0] if len(msg.velocity) > 0 else 0.0
            v2 = msg.velocity[1] if len(msg.velocity) > 1 else 0.0
            p1 = msg.position[0] if len(msg.position) > 0 else 0.0
            p2 = msg.position[1] if len(msg.position) > 1 else 0.0

            dato = {
                "velocidad_1": round(v1, 4),
                "posicion_1":  round(p1, 4),
                "velocidad_2": round(v2, 4),
                "posicion_2":  round(p2, 4),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timestamp_firestore": SERVER_TIMESTAMP,
            }

            # Actualizar el dato más reciente de forma thread-safe
            with self._lock:
                self._ultimo_dato = dato

            self.get_logger().debug(
                f'[State] V1={v1:.2f} P1={p1:.2f} | V2={v2:.2f} P2={p2:.2f}'
            )

        except Exception as e:
            self.get_logger().error(f'[State] Error procesando mensaje: {e}')

    # ============================================================
    # TIMER CALLBACK
    # Se ejecuta cada 1 segundo y decide si es momento de subir.
    # ============================================================
    def timer_callback(self):
        ahora = time.time()

        # Verificar si pasó el intervalo mínimo
        if ahora - self._ultimo_envio < INTERVALO_SUBIDA_SEG:
            return

        # Obtener el último dato de forma thread-safe
        with self._lock:
            dato = self._ultimo_dato

        if dato is None:
            self.get_logger().warn('[Firebase] Sin datos aún — esperando primer mensaje de esp32/state.')
            return

        # Subir a Firebase
        self._guardar_lectura(dato)
        self._ultimo_envio = ahora

    # ============================================================
    # ESCRITURA EN FIRESTORE
    # ============================================================
    def _guardar_lectura(self, datos: dict):
        """
        Guarda los datos en dos lugares de Firestore:
          1. motores/lecturas/historial/{auto-id}  → registro histórico
          2. motores/estado_actual                 → estado en tiempo real
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
                f'V1={datos["velocidad_1"]} P1={datos["posicion_1"]} | '
                f'V2={datos["velocidad_2"]} P2={datos["posicion_2"]}'
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