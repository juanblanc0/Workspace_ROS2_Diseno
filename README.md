# Inicializar nodo comunicacion UART

colcon build
source install/setup.bash
ros2 run package1 esp32_node

# Inicializar nodo para planeacion de trayectorias

colcon build
source install/setup.bash
ros2 run package1 trajectory_sender

# Inicializar nodo para comunicacion con Firebase

colcon build
source install/setup.bash
ros2 run package1 firebase_node

# Comando para enviar trayectoria

ros2 topic pub --once /trajectory_set std_msgs/msg/Int32 "{data: 1}"
