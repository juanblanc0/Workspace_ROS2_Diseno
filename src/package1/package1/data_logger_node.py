#!/usr/bin/env python3
"""
data_logger_node.py

Buffer circular de 500 datos. Graba en CSV:
  - Valores reales del motor  (esp32/state)
  - Setpoint de posición      (/traj_setpoint)
  - Velocidad comandada       (esp32/cmd_vel)
  - Set activo                (/trajectory_set)

El CSV se reescribe completo en cada nuevo dato de esp32/state.
Los setpoints se toman del último valor conocido al momento de cada escritura.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile

from sensor_msgs.msg import JointState
from std_msgs.msg import Int32
from geometry_msgs.msg import Vector3

import csv
import os
from collections import deque


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

LOG_DIR        = '/home/camilo/logs'
LOG_FILENAME   = 'motor_data.csv'
BUFFER_SIZE    = 500
STATE_TOPIC    = 'esp32/state'
SET_TOPIC      = '/trajectory_set'
SETPOINT_TOPIC = '/traj_setpoint'
CMDVEL_TOPIC   = 'esp32/cmd_vel'

CSV_HEADER = [
    'timestamp_s',
    'elapsed_s',
    'set_activo',
    # Valores reales
    'vel_motor1',
    'pos_motor1',
    'vel_motor2',
    'pos_motor2',
    # Setpoint de posición deseada (del CSV de trayectoria via /traj_setpoint)
    'sp_pos_motor1',
    'sp_pos_motor2',
    # Velocidad comandada a la ESP32 (feedforward + corrección)
    'sp_vel_motor1',
    'sp_vel_motor2',
]


# ─────────────────────────────────────────────────────────────────────────────
# Nodo
# ─────────────────────────────────────────────────────────────────────────────

class DataLoggerNode(Node):

    def __init__(self):
        super().__init__('data_logger_node')

        qos = QoSProfile(depth=10)

        # ── Buffer circular ───────────────────────────────────────────────────
        self._buffer: deque[list] = deque(maxlen=BUFFER_SIZE)

        # ── Archivo CSV ───────────────────────────────────────────────────────
        os.makedirs(LOG_DIR, exist_ok=True)
        self._log_path = os.path.join(LOG_DIR, LOG_FILENAME)
        self._write_csv()

        # ── Tiempo de inicio ──────────────────────────────────────────────────
        self._start_time = self.get_clock().now()

        # ── Últimos valores conocidos de setpoints ────────────────────────────
        # Se actualizan independientemente cuando llega cada topic.
        # Al escribir la fila del JointState se usan los últimos recibidos.
        self._set_activo = 0
        self._sp_pos_q1  = 0.0
        self._sp_pos_q2  = 0.0
        self._sp_vel_q1  = 0.0
        self._sp_vel_q2  = 0.0

        # ── Subscribers ───────────────────────────────────────────────────────
        self.create_subscription(
            JointState, STATE_TOPIC,    self._state_callback,    qos)
        self.create_subscription(
            Int32,      SET_TOPIC,      self._set_callback,      qos)
        self.create_subscription(
            Vector3,    SETPOINT_TOPIC, self._setpoint_callback, qos)
        self.create_subscription(
            Vector3,    CMDVEL_TOPIC,   self._cmdvel_callback,   qos)

        self.get_logger().info(
            f'DataLoggerNode iniciado.\n'
            f'  CSV    : {self._log_path}\n'
            f'  Buffer : {BUFFER_SIZE} datos (circular)\n'
            f'  Topics : {STATE_TOPIC}, {SET_TOPIC},\n'
            f'           {SETPOINT_TOPIC}, {CMDVEL_TOPIC}'
        )

    # =========================================================================
    # ESCRITURA DEL CSV
    # =========================================================================
    def _write_csv(self):
        """Sobreescribe el CSV completo con el contenido actual del buffer."""
        with open(self._log_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            writer.writerows(self._buffer)

    # =========================================================================
    # CALLBACKS DE SETPOINTS (solo actualizan el último valor conocido)
    # =========================================================================
    def _set_callback(self, msg: Int32):
        self._set_activo = msg.data

    def _setpoint_callback(self, msg: Vector3):
        """Posición deseada publicada por trajectory_sender en /traj_setpoint."""
        self._sp_pos_q1 = msg.x
        self._sp_pos_q2 = msg.y

    def _cmdvel_callback(self, msg: Vector3):
        """Velocidad comandada (feedforward + corrección proporcional)."""
        self._sp_vel_q1 = msg.x
        self._sp_vel_q2 = msg.y

    # =========================================================================
    # CALLBACK PRINCIPAL: estado real → genera fila en el CSV
    # =========================================================================
    def _state_callback(self, msg: JointState):
        """
        Cada JointState recibido genera una fila en el CSV.
        Los setpoints se toman del último valor conocido al momento de esta llamada,
        por lo que están sincronizados temporalmente con la muestra real.
        """
        if len(msg.position) < 2 or len(msg.velocity) < 2:
            self.get_logger().warn('JointState incompleto. Ignorando.')
            return

        now         = self.get_clock().now()
        timestamp_s = now.nanoseconds / 1e9
        elapsed_s   = (now - self._start_time).nanoseconds / 1e9

        self._buffer.append([
            f'{timestamp_s:.6f}',
            f'{elapsed_s:.6f}',
            self._set_activo,
            # Reales
            f'{msg.velocity[0]:.6f}',
            f'{msg.position[0]:.6f}',
            f'{msg.velocity[1]:.6f}',
            f'{msg.position[1]:.6f}',
            # Setpoints
            f'{self._sp_pos_q1:.6f}',
            f'{self._sp_pos_q2:.6f}',
            f'{self._sp_vel_q1:.6f}',
            f'{self._sp_vel_q2:.6f}',
        ])

        self._write_csv()

    # =========================================================================
    # DESTRUCTOR
    # =========================================================================
    def destroy_node(self):
        self._write_csv()
        self.get_logger().info(f'CSV cerrado: {self._log_path}')
        super().destroy_node()


# =============================================================================
# MAIN
# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node = DataLoggerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()