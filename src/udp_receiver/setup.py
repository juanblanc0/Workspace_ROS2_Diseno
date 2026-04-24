from setuptools import find_packages, setup

package_name = 'udp_receiver'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Juan',
    maintainer_email='tu_correo@umng.edu.co',
    description='Nodo ROS2 Jazzy para recepción de datos por socket UDP',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            # Formato: nombre_ejecutable = paquete.modulo:funcion_main
            'tcp_receiver_node = udp_receiver.tcp_receiver_node:main',
        ],
    },
)