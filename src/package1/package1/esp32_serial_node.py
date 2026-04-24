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

        port = self.get_parameter('port').value
        baudrate = int(self.get_parameter('baudrate').value or 115200)

        # Inicializar serial
        try:
            self.ser = serial.Serial(port, baudrate, timeout=1)
            self.get_logger().info(f'Conectado a {port} @ {baudrate}')
        except Exception as e:
            self.get_logger().error(f'Error abriendo serial: {e}')
            raise

        # Publisher: estado motores
        self.state_pub = self.create_publisher(
            JointState,
            'esp32/state',
            qos
        )

        # Subscriber: comando velocidad
        self.cmd_sub = self.create_subscription(
            Vector3,
            'esp32/cmd_vel',
            self.cmd_callback,
            qos
        )

        # Hilo lectura serial
        self.thread = threading.Thread(target=self.read_serial)
        self.thread.daemon = True
        self.thread.start()

    # =========================
    # LECTURA DESDE ESP32
    # =========================
    def read_serial(self):
        while rclpy.ok():
            try:
                line = self.ser.readline().decode('utf-8').strip()

                # Esperado: V1,P1,V2,P2
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

            except Exception as e:
                self.get_logger().error(f'Error lectura serial: {e}')

    # =========================
    # ENVÍO HACIA ESP32
    # =========================
    def cmd_callback(self, msg):
        try:
            v1 = msg.x
            v2 = msg.y

            # Formato: v1,v2\n
            data = f"{v1},{v2}\n"
            self.ser.write(data.encode('utf-8'))

            self.get_logger().info(
                f'Cmd -> M1:{v1:.2f} rad/s | M2:{v2:.2f} rad/s'
            )

        except Exception as e:
            self.get_logger().error(f'Error enviando serial: {e}')


# =========================
# MAIN
# =========================
def main(args=None):
    rclpy.init(args=args)

    node = ESP32SerialNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass

    node.destroy_node()
    rclpy.shutdown()