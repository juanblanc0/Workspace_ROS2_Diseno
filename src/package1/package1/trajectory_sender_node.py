#!/usr/bin/env python3
"""
trajectory_sender_node.py

Nodo ROS2 Jazzy para enviar trayectorias a la ESP32 via el topic esp32/cmd_vel.

Uso:
  Publicar un Int32 al topic /trajectory_set con valor 1, 2 o 3
  para ejecutar el set correspondiente.

Sets:
  Set 1: T1, T2
  Set 2: T3, T4, T5, T6
  Set 3: T7, T8, T9, T10

Cada trayectoria tiene 51 iteraciones. Las velocidades se envían cada 20ms.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile

from std_msgs.msg import Int32
from geometry_msgs.msg import Vector3

import csv
import os
import threading


# ─────────────────────────────────────────────
# Definición de los sets de trayectorias
# ─────────────────────────────────────────────
TRAJECTORY_SETS = {
    1: [1, 2],
    2: [3, 4, 5, 6],
    3: [7, 8, 9, 10],
}

TRAJ_DIR = '/home/camilo/trayectorias'
PUBLISH_INTERVAL_S = 0.02   # 20 ms
TOTAL_ITERATIONS   = 51


class TrajectorySenderNode(Node):
    """
    Nodo que recibe un número de set (1/2/3) por topic y publica
    las velocidades de cada trayectoria del set hacia esp32/cmd_vel.
    """

    def __init__(self):
        super().__init__('trajectory_sender_node')

        qos = QoSProfile(depth=10)

        # ── Subscriber: recibe el número de set ──────────────────────────────
        self.set_sub = self.create_subscription(
            Int32,
            '/trajectory_set',
            self.set_callback,
            qos
        )

        # ── Publisher: envía velocidades al nodo ESP32 ───────────────────────
        self.cmd_pub = self.create_publisher(
            Vector3,
            'esp32/cmd_vel',
            qos
        )

        # ── Estado interno ────────────────────────────────────────────────────
        self._running    = False   # True mientras se ejecuta un set
        self._stop_event = threading.Event()
        self._exec_thread: threading.Thread | None = None

        self.get_logger().info('TrajectorySenderNode iniciado. '
                               'Publica en /trajectory_set (Int32) '
                               'el número de set (1, 2 o 3).')

    # =========================================================================
    # CALLBACK: llegó un número de set
    # =========================================================================
    def set_callback(self, msg: Int32):
        set_id = msg.data

        if set_id not in TRAJECTORY_SETS:
            self.get_logger().error(
                f'Set "{set_id}" no válido. Usa 1, 2 o 3.')
            return

        # Si ya hay un set corriendo, lo detenemos limpiamente
        if self._running:
            self.get_logger().warn(
                f'Interrumpiendo set en curso para iniciar Set {set_id}.')
            self._stop_event.set()
            if self._exec_thread is not None:
                self._exec_thread.join(timeout=2.0)

        # Reiniciar evento y lanzar hilo de ejecución
        self._stop_event.clear()
        self._running = True
        self._exec_thread = threading.Thread(
            target=self._run_set,
            args=(set_id,),
            daemon=True
        )
        self._exec_thread.start()

    # =========================================================================
    # EJECUCIÓN DEL SET (corre en hilo separado)
    # =========================================================================
    def _run_set(self, set_id: int):
        traj_indices = TRAJECTORY_SETS[set_id]
        self.get_logger().info(
            f'▶ Iniciando Set {set_id} | '
            f'Trayectorias: {["T"+str(i) for i in traj_indices]}')

        for traj_idx in traj_indices:
            if self._stop_event.is_set():
                self.get_logger().warn('Set interrumpido antes de completarse.')
                break

            # ── Cargar CSV de velocidades ─────────────────────────────────
            vel_file = os.path.join(TRAJ_DIR, f'T{traj_idx}_p.csv')

            try:
                velocities = self._load_velocities(vel_file)
            except FileNotFoundError:
                self.get_logger().error(
                    f'Archivo no encontrado: {vel_file}. Abortando set.')
                break
            except Exception as e:
                self.get_logger().error(
                    f'Error leyendo {vel_file}: {e}. Abortando set.')
                break

            if len(velocities) != TOTAL_ITERATIONS:
                self.get_logger().warn(
                    f'T{traj_idx}_p tiene {len(velocities)} filas '
                    f'(se esperaban {TOTAL_ITERATIONS}). Se usarán las disponibles.')

            self.get_logger().info(
                f'  → Ejecutando T{traj_idx} '
                f'({len(velocities)} iteraciones × {PUBLISH_INTERVAL_S*1000:.0f} ms)')

            # ── Publicar velocidades a 20ms ───────────────────────────────
            for i, (q1_p, q2_p) in enumerate(velocities):
                if self._stop_event.is_set():
                    self.get_logger().warn(
                        f'Trayectoria T{traj_idx} interrumpida en iter {i+1}.')
                    break

                vec_msg = Vector3()
                vec_msg.x = q1_p
                vec_msg.y = q2_p
                vec_msg.z = 0.0
                self.cmd_pub.publish(vec_msg)

                self.get_logger().debug(
                    f'T{traj_idx} | iter {i+1:02d} | '
                    f'q1_p={q1_p:.6f}  q2_p={q2_p:.6f}')

                # Espera de 20 ms (o sale antes si se solicita stop)
                interrupted = self._stop_event.wait(timeout=PUBLISH_INTERVAL_S)
                if interrupted:
                    break

            else:
                # Bucle completado sin break → trayectoria terminada
                self.get_logger().info(f'  ✓ T{traj_idx} completada.')

        if not self._stop_event.is_set():
            self.get_logger().info(f'✅ Set {set_id} completado.')

        self._running = False

    # =========================================================================
    # CARGA DE CSV DE VELOCIDADES
    # =========================================================================
    def _load_velocities(self, filepath: str) -> list[tuple[float, float]]:
        """
        Lee un archivo CSV de velocidades y devuelve una lista de
        tuplas (q1_p, q2_p) ordenadas por iteración.

        Formato esperado del CSV:
            iteracion,q1_p,q2_p
            1,0,-0
            2,0,-0.062832
            ...
        """
        velocities = []

        with open(filepath, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)

            # Validar que las columnas requeridas existan
            required_cols = {'iteracion', 'q1_p', 'q2_p'}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f'El CSV {filepath} no tiene las columnas esperadas. '
                    f'Columnas encontradas: {reader.fieldnames}')

            rows = sorted(reader, key=lambda r: int(r['iteracion']))

            for row in rows:
                q1_p = float(row['q1_p'])
                q2_p = float(row['q2_p'])
                velocities.append((q1_p, q2_p))

        return velocities


# =============================================================================
# MAIN
# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node = TrajectorySenderNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Detener cualquier trayectoria en curso
        node._stop_event.set()
        if node._exec_thread is not None:
            node._exec_thread.join(timeout=2.0)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()