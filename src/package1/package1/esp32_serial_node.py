import rclpy
from rclpy.node import Node
import serial
import threading
from geometry_msgs.msg import Vector3
from sensor_msgs.msg import JointState
from rclpy.qos import QoSProfile


class ESP32SerialNode(Node):
    def __init__(self):
        super().__init__('esp32_serial_node')

        # QoS (estable para control)
        qos = QoSProfile(depth=10)

        # Parámetros configurables
        self.declare_parameter('port', '/dev/ttyUSB0')
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

        # ── Publishers / Subscribers ──────────────────────────────────────────
        self.state_pub = self.create_publisher(
            JointState,
            'esp32/state',
            qos
        )
        self.cmd_sub = self.create_subscription(
            Vector3,
            'esp32/cmd_vel',
            self.cmd_callback,
            qos
        )

        # ── Hilo de lectura (siempre corriendo, espera conexión internamente) ──
        self.thread = threading.Thread(target=self.read_serial, daemon=True)
        self.thread.start()

        # ── Primer intento de conexión ────────────────────────────────────────
        self.get_logger().info(
            f'Intentando conectar a ESP32 en {self.port} @ {self.baudrate} baud...'
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
                f'✅ ESP32 conectada correctamente en {self.port} @ {self.baudrate} baud'
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
            f'❌ Conexión perdida con ESP32. '
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

                # Formato esperado desde ESP32: V1,P1,V2,P2
                if line:
                    parts = line.split(',')
                    if len(parts) == 4:
                        v1 = float(parts[0])
                        p1 = float(parts[1])
                        v2 = float(parts[2])
                        p2 = float(parts[3])

                        msg = JointState()
                        msg.header.stamp = self.get_clock().now().to_msg()
                        msg.name = ['motor1', 'motor2']
                        msg.position = [p1, p2]
                        msg.velocity = [v1, v2]
                        self.state_pub.publish(msg)

                        self.get_logger().info(
                            f'M1 -> v:{v1:.2f}, p:{p1:.2f} | '
                            f'M2 -> v:{v2:.2f}, p:{p2:.2f}'
                        )

            except (serial.SerialException, OSError) as e:
                # Error de hardware/comunicación: la ESP32 se desconectó
                self.get_logger().error(f'Conexión serial perdida: {e}')
                self.handle_disconnection()

            except Exception as e:
                # Error de parseo u otro: no es una desconexión, solo loguear
                self.get_logger().warn(f'Error procesando datos serial: {e}')

    # =========================================================================
    # ENVÍO HACIA ESP32
    # =========================================================================

    def cmd_callback(self, msg):
        """
        Recibe comandos de velocidad y los envía a la ESP32.
        Si no hay conexión activa, descarta el comando y avisa.
        """
        if not self.is_connected or self.ser is None:
            self.get_logger().warn(
                'Comando recibido pero ESP32 no conectada. Descartando.'
            )
            return

        try:
            v1 = msg.x
            v2 = msg.y
            data = f"{v1},{v2}\n"

            with self.serial_lock:
                if self.ser is not None:
                    self.ser.write(data.encode('utf-8'))

            self.get_logger().info(
                f'Cmd -> M1:{v1:.2f} rad/s | M2:{v2:.2f} rad/s'
            )

        except (serial.SerialException, OSError) as e:
            self.get_logger().error(f'Error enviando serial: {e}')
            self.handle_disconnection()

        except Exception as e:
            self.get_logger().error(f'Error en cmd_callback: {e}')


# =============================================================================
# MAIN
# =============================================================================

def main(args=None):
    rclpy.init(args=args)
    node = ESP32SerialNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()