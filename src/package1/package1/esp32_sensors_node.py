import rclpy
from rclpy.node import Node
import serial
import threading
from geometry_msgs.msg import Vector3
from rclpy.qos import QoSProfile


class ESP32SensorsNode(Node):
    def __init__(self):
        super().__init__('esp32_sensors_node')

        # QoS (estable para lectura de sensores)
        qos = QoSProfile(depth=10)

        # Parámetros configurables
        self.declare_parameter('port', '/dev/ttyUSB1')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('retry_interval_sec', 3.0)

        self.port = self.get_parameter('port').value
        self.baudrate = int(self.get_parameter('baudrate').value or 115200)
        self.retry_interval = float(
            self.get_parameter('retry_interval_sec').value or 3.0
        )

        # ── Estado de conexión ────────────────────────────────────────────────
        self.ser = None
        self.is_connected = False
        self.reconnect_timer = None
        self.serial_lock = threading.Lock()  # protege acceso a self.ser entre hilos

        # ── Publisher ─────────────────────────────────────────────────────────
        # Vector3: x = s1, y = s2, z = s3
        self.sensor_pub = self.create_publisher(
            Vector3,
            'esp32/sensors',
            qos
        )

        # ── Hilo de lectura (siempre corriendo, espera conexión internamente) ──
        self.thread = threading.Thread(target=self.read_serial, daemon=True)
        self.thread.start()

        # ── Primer intento de conexión ────────────────────────────────────────
        self.get_logger().info(
            f'Intentando conectar a ESP32 sensores en {self.port} @ {self.baudrate} baud...'
        )
        self.try_connect()

    # =========================================================================
    # CONEXIÓN Y RECONEXIÓN
    # =========================================================================

    def try_connect(self):
        """
        Intenta abrir el puerto serial.
        - Si tiene éxito: cancela el timer de reconexión, marca is_connected = True.
        - Si falla: programa un timer para reintentar cada retry_interval segundos.
        """
        try:
            new_ser = serial.Serial(self.port, self.baudrate, timeout=1)

            with self.serial_lock:
                self.ser = new_ser
                self.is_connected = True

            # Cancelar timer de reconexión si existía
            if self.reconnect_timer is not None:
                self.reconnect_timer.cancel()
                self.reconnect_timer = None

            self.get_logger().info(
                f'✅ ESP32 sensores conectada correctamente en {self.port} @ {self.baudrate} baud'
            )

        except serial.SerialException as e:
            self.is_connected = False

            self.get_logger().warn(
                f'⚠️  No se pudo conectar a {self.port}: {e}. '
                f'Reintentando en {self.retry_interval:.1f}s...'
            )

            # Crear timer de reconexión solo si no existe ya uno activo
            if self.reconnect_timer is None:
                self.reconnect_timer = self.create_timer(
                    self.retry_interval,
                    self.try_connect
                )

    def handle_disconnection(self):
        """
        Llamado cuando se detecta una pérdida de conexión DURANTE la operación.
        Cierra el puerto limpiamente y lanza el proceso de reconexión automática.
        """
        with self.serial_lock:
            if not self.is_connected:
                # Ya se estaba manejando, evitar llamadas duplicadas
                return
            self.is_connected = False
            try:
                if self.ser is not None:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None

        self.get_logger().error(
            f'❌ Conexión perdida con ESP32 sensores. '
            f'Reintentando en {self.retry_interval:.1f}s...'
        )

        # Lanzar timer de reconexión si no hay uno activo
        if self.reconnect_timer is None:
            self.reconnect_timer = self.create_timer(
                self.retry_interval,
                self.try_connect
            )

    # =========================================================================
    # LECTURA DESDE ESP32
    # =========================================================================

    def read_serial(self):
        """
        Hilo de lectura. Corre siempre en background.
        Si no hay conexión, duerme y vuelve a intentar.
        Si la conexión se pierde durante la lectura, llama a handle_disconnection().

        Formato esperado desde ESP32: s1,s2,s3
        Publicado como Vector3: x=s1, y=s2, z=s3
        """
        while rclpy.ok():
            # Si no hay conexión, esperar sin bloquear el hilo de ROS2
            if not self.is_connected or self.ser is None:
                import time
                time.sleep(0.5)
                continue

            try:
                with self.serial_lock:
                    if self.ser is None:
                        continue
                    line = self.ser.readline().decode('utf-8').strip()

                # Formato esperado desde ESP32: s1,s2,s3
                if line:
                    parts = line.split(',')
                    if len(parts) == 3:
                        s1 = float(parts[0])
                        s2 = float(parts[1])
                        s3 = float(parts[2])

                        msg = Vector3()
                        msg.x = s1
                        msg.y = s2
                        msg.z = s3
                        self.sensor_pub.publish(msg)

                        self.get_logger().info(
                            f'Sensores -> S1:{s1:.4f} | S2:{s2:.4f} | S3:{s3:.4f}'
                        )

            except (serial.SerialException, OSError) as e:
                # Error de hardware/comunicación: la ESP32 se desconectó
                self.get_logger().error(f'Conexión serial perdida: {e}')
                self.handle_disconnection()

            except Exception as e:
                # Error de parseo u otro: no es una desconexión, solo loguear
                self.get_logger().warn(f'Error procesando datos serial: {e}')


# =============================================================================
# MAIN
# =============================================================================

def main(args=None):
    rclpy.init(args=args)
    node = ESP32SensorsNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()