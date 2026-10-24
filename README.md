# Residencias

Aplicación Streamlit para registrar ingresos en las residencias Carmen Luisa y
las Acacias, consultar residentes y controlar vencimientos.

## Ejecutar

Los importes en bolívares y dólares se ingresan y muestran con punto de miles
y coma decimal, por ejemplo `1.234,56`. Los montos tienen dos decimales; las
tasas de cambio conservan su precisión. También se admiten importes simples
con punto decimal de uno o dos dígitos (por ejemplo `150.50`) por compatibilidad,
pero se muestran como `150,50`. Los recibos PDF ya guardados no se reescriben.

```sh
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Al guardar cambios en los archivos Python, Streamlit vuelve a ejecutar la
aplicación automáticamente. Si el servidor ya estaba iniciado cuando se creó
esta configuración, reinícialo una vez para que el cambio surta efecto.

## Publicar una demostración en Streamlit Community Cloud

1. Confirma que el código que quieres publicar está subido a GitHub en la rama
   elegida. No subas datos privados ni secretos: `data/` y
   `.streamlit/secrets.toml` están excluidos de Git.
2. Entra en [Streamlit Community Cloud](https://share.streamlit.io) con GitHub,
   autoriza el acceso al repositorio y pulsa **Create app**.
3. Selecciona el repositorio `oscarriera201178-netizen/Residencias`, la rama
   correspondiente y `app.py` como **Main file path**.
4. En **Advanced settings**, elige una versión de Python compatible y configura
   solo los secretos que realmente necesite la aplicación. Nunca guardes
   contraseñas en el código ni en GitHub.
5. Pulsa **Deploy** y espera a que termine la instalación de dependencias y el
   inicio de la aplicación. Abre la URL asignada para acceder al portal.

**Publicar la aplicación no configura almacenamiento persistente por defecto.**
Community Cloud no garantiza que los archivos escritos por la aplicación se
conserven tras reinicios o nuevos despliegues, así que sin configuración
adicional las bases SQLite locales se pierden en cada reinicio.

## Persistencia en producción con Turso

Para que las cuentas de usuario y la información de las residentes permanezcan
guardadas en la nube, el portal puede sincronizar sus bases SQLite con
[Turso](https://turso.tech) (plan gratuito). Configúralo una sola vez:

1. Crea una cuenta en Turso y crea una base de datos (por ejemplo
   `residencias`).
2. Obtén la URL de la base (`libsql://....turso.io`) y genera un token de
   acceso desde el panel de Turso o con la CLI: `turso db tokens create
   residencias`.
3. En Streamlit Community Cloud abre tu app, entra en **Settings → Secrets** y
   pega:

   ```toml
   [turso]
   url = "libsql://tu-base.turso.io"
   token = "tu-token-de-acceso"

   [auth]
   programmer_password = "una-clave-larga-y-segura"
   administrator_password = "otra-clave-larga-y-segura"
   ```

   La sección `[auth]` crea automáticamente las cuentas **Programador** y
   **Administrador** la primera vez que arranca la aplicación; ya no tendrás
   que activarlas tras cada reinicio. Solo quien edite estos secrets puede
   cambiarlas. Opcionalmente puedes fijar `programmer_username` y
   `administrator_username` (mínimo 12 caracteres las contraseñas).
4. Guarda y pulsa **Reboot** en el menú ⋮ de la app. En el primer arranque se
   crean las tablas y las cuentas; desde entonces todo queda guardado en Turso.

Sin esas credenciales la aplicación sigue funcionando con el SQLite local
(desarrollo y pruebas). Nunca subas los secrets al repositorio.

Usa solo datos ficticios para una demostración pública; antes de cargar
información real, prepara copias de seguridad y revisa quién puede acceder a la
aplicación.

## Inicio de sesión y perfiles

El portal siempre exige autenticación antes de mostrar residentes, pagos o
descargas. No tiene usuarios ni contraseñas predeterminados ni modo público.
La primera vez, establece la contraseña directamente en el portal mediante
**Nueva contraseña** y **Confirmar contraseña**, sin código de activación.
**Completa esta configuración en un entorno privado antes de publicar el
portal**: mientras no exista la cuenta inicial, cualquier persona con acceso
a esa pantalla podría crearla y acceder a los datos.
Solo se permite crear una cuenta inicial, incluso ante envíos simultáneos.
No escribas contraseñas en el código, GitHub ni el chat.
El usuario queda fijado como **Programador**
(el inicio de sesión no distingue mayúsculas/minúsculas).
La contraseña elegida es permanente hasta que decidas cambiarla o recuperarla;
no requiere cambio en el primer acceso. La pantalla inicial desaparece al
activar la cuenta y no permite reemplazar su contraseña desde esa pantalla.
También está disponible `python portal_auth.py bootstrap` en una terminal
privada, antes de iniciar el servidor, para crear la cuenta sin exponer el
formulario inicial en internet.

| Perfil | Máximo de cuentas | Permisos |
| --- | --- | --- |
| ADMINISTRADOR PROGRAMADOR | 1 | Todas las operaciones y administración de usuarios |
| ADMINISTRADOR | 1 | Ingresos, pagos, facturas, saldos iniciales y bajas; sin administrar usuarios |
| USUARIO VISUALIZAR | 2 | Consulta de ambas residencias y descarga de planillas, recibos y reportes; sin modificar datos |

El programador usa **Administración de usuarios** para crear las otras tres
cuentas, activar/desactivar su acceso y restablecer contraseñas temporales.
Las cuentas inactivas también ocupan su cupo: se reutiliza la cuenta existente
en lugar de crear otra. No se puede crear otro programador desde el portal ni
desactivar su propia cuenta. Todos acceden a ambas residencias.
El **ADMINISTRADOR** debe elegir **Residencia Carmen Luisa** o
**Residencia Las Acacias** después de autenticarse y cambiar su contraseña
temporal, y pulsar el botón principal **Ingresar al área** antes de consultar
registros.
Solo se muestra y se procesa el área elegida. **Cambiar de área**, en la barra
lateral, vuelve a la selección sin cerrar sesión y limpia formularios, búsquedas
y borradores de la sesión para evitar mezclar las áreas. Al cerrar sesión debe
elegir nuevamente.
El programador y los usuarios de consulta mantienen ambas residencias visibles.
El nombre de la cuenta autenticada se muestra en la cabecera superior del portal.

Las contraseñas tienen entre 12 y 128 caracteres y se guardan mediante
**scrypt con una sal aleatoria por contraseña**, nunca en texto claro.
Entrega las contraseñas temporales por un canal privado: cada usuario debe
cambiarlas al entrar antes de poder consultar datos. Todos pueden cambiar su
propia contraseña desde la barra lateral. Si el programador pierde la suya,
el responsable del servidor puede ejecutar
`python portal_auth.py recover-programmer` en su terminal privada.

Después de cinco intentos fallidos, la cuenta se bloquea durante 15 minutos.
Las sesiones vencen a los 30 minutos sin interacción o a las ocho horas de
iniciadas. Cerrar sesión, cambiar/restablecer contraseña o desactivar la cuenta
revoca las sesiones correspondientes. Al recargar completamente el navegador
o reiniciar el portal puede ser necesario entrar nuevamente. Los permisos se
verifican en el servidor: ocultar botones no es la única protección.

### Preparación del servidor de producción

- Publica solo detrás de **HTTPS**, con WebSocket habilitado para Streamlit.
  No expongas el puerto de desarrollo ni la carpeta de datos como archivos web.
- Configura `RESIDENCIAS_DATA_DIR` con la ruta absoluta de un **disco persistente
  privado**, antes de iniciar el portal, el comando de cuentas y los recordatorios.
  Sin esa variable se utiliza `data/` junto al proyecto.
- El almacenamiento contiene `residencias.sqlite3` (registros, fotos, PDFs y
  pagos) y `auth.sqlite3` (cuentas, hashes y sesiones). Ambos quedan fuera de Git.
  Traslada los datos existentes de forma privada, con los procesos detenidos,
  antes de configurar la ruta nueva. No crees una base vacía por error al cambiarla.
- Respalda ambas bases con la API de copia de seguridad de SQLite o con los
  procesos detenidos. Guarda copias cifradas fuera del servidor y verifica su
  restauración. No publiques respaldos en GitHub. **La autenticación no configura
  automáticamente un servidor, dominio, HTTPS ni respaldos programados.**
- Conserva las credenciales SMTP en el gestor de secretos del alojamiento;
  no en archivos versionados. `.env` y `.streamlit/secrets.toml` están excluidos.
- El proceso independiente de recordatorios necesita acceso local confiable al
  disco y al secreto SMTP; no necesita iniciar sesión como un usuario del portal.
  Protege también el acceso a la terminal y los permisos del directorio.

## Tasa oficial del dólar

Al iniciar sesión, el cintillo del portal consulta la tasa USD publicada por el
[Banco Central de Venezuela](https://www.bcv.org.ve/estadisticas/tipo-de-cambio-de-referencia-0)
y muestra su fecha de vigencia. El resultado se conserva en caché durante
cinco minutos para evitar consultas repetidas. Si el BCV no está disponible,
el portal avisa que no pudo consultar la tasa; los pagos en bolívares siguen
requiriendo que se registre manualmente la tasa y fecha correspondientes.
La consulta HTTPS conserva la validación de certificados y añade el
intermediario Sectigo R36 que el servidor del BCV omite en su cadena TLS.

## Control de Gastos

Cada residencia tiene un área **Control de Gastos**. Los perfiles con permisos
de escritura pueden registrar gastos; los perfiles de consulta solo pueden
ver el historial y los saldos.

- El concepto se introduce manualmente. Se registra la moneda (bolívares o
  dólares), el total pagado y la fecha. Para gastos con factura, la fecha de
  factura y pago es la misma y el número de factura es obligatorio.
- El historial y el PDF muestran primero la fecha de registro y después la
  fecha de factura / gasto. La segunda columna contiene la fecha de la factura
  si existe, o la fecha del gasto si no hay factura. En registros históricos
  anteriores a este cambio, la fecha de registro se indica como no disponible
  porque no se almacenaba.
- Después aparecen el número de factura, concepto, base imponible, IVA del
  16 %, total pagado, tipo de moneda y banco/medio.
- El total **ya incluye el IVA del 16 %**: la base se obtiene dividiendo el
  total entre 1,16 y el IVA se desglosa automáticamente, sin aumentar el total.
  Los gastos sin factura no tienen número de factura ni IVA desglosado.
- La opción de factura solo está habilitada para gastos en bolívares. Los
  gastos pagados en dólares se registran sin factura ni IVA desglosado.
- En bolívares se selecciona el banco de salida (Banco de Venezuela, Banco
  del Tesoro o Fondo Común). En dólares se elige Efectivo, Binance o Zelle.
  No se convierte dinero entre cuentas automáticamente.
- El gasto se descuenta únicamente del banco/medio y residencia indicados.
  Los saldos disponibles suman saldos iniciales y cobros identificados, y
  restan gastos y devoluciones. Los cobros antiguos sin importe, moneda o
  banco/medio identificado no financian gastos. No son saldos obtenidos
  directamente del banco: deben conciliarse con los estados de cuenta.
- Se rechazan gastos que dejen saldo negativo, tanto en la fecha indicada
  como en fechas posteriores ya registradas. La validación y el registro se
  realizan dentro de una misma transacción.
- **Registrar saldo inicial** permite registrar un saldo de arranque por
  moneda y banco/medio, reutilizando el almacenamiento existente. No debe
  incluir cobros que ya estén registrados para evitar duplicarlos.
- El botón **Descargar control de gastos (PDF)** genera un reporte imprimible
  del historial de la residencia, con fechas, facturas, base imponible, IVA,
  total y totales por moneda. Está disponible también para perfiles
  de consulta y cuando el historial está vacío.
- El informe mensual y su PDF conservan los acumulados de cobros y añaden
  una sección **Saldos disponibles al cierre del informe** que calcula los pagos
  recibidos en el mes menos los gastos del mismo mes, por moneda y banco/medio,
  más el saldo neto de meses anteriores y los saldos de arranque registrados
  durante el mes, sin contarlos dos veces. Si los movimientos producen un
  resultado negativo, se avisa que deben conciliarse en lugar de mostrarlo como
  dinero disponible o sustituirlo por cero. En Control de Gastos,
  los saldos disponibles al día de hoy sí descuentan los gastos registrados hasta
  la fecha, junto con las devoluciones.

Los gastos se guardan en la tabla `expenses`, en la misma base local o réplica
Turso que los residentes. La tabla se crea al iniciar la versión actualizada;
publicar el código no elimina ni reinicia los datos existentes.

## Pago de IVA (16%)

Cada residencia tiene un botón **Pago de IVA (16%)**, con selector de mes y
actividades ordenadas cronológicamente por fecha del cobro o factura del gasto.
El informe muestra fecha, número de factura, concepto, importe, débito fiscal
y crédito fiscal, exclusivamente para movimientos en bolívares con factura:

- El débito corresponde a cobros con número de factura (mensualidades,
  depósitos y servicios, según la selección del portal). Usa el mismo cálculo
  del IVA de cobros: 16 % del importe registrado.
- El crédito corresponde al IVA incluido y guardado en los gastos con factura.
- El resultado es **débito fiscal menos crédito fiscal**. Si es negativo,
  se muestra un saldo a favor; no se permite registrar un pago ni se arrastra
  automáticamente el crédito a otros meses.
- Los perfiles de escritura pueden registrar el pago completo del resultado
  positivo, indicando banco, fecha y referencia y confirmando que ya se pagó.
  Esto no realiza una transferencia bancaria.
- Solo se admite un pago por residencia y mes. El registro guarda los totales
  utilizados y crea un gasto sin factura ni crédito fiscal, dentro de la misma
  transacción. Se descuenta del banco y aparece en Control de Gastos y en los
  saldos del mes de pago, sin descontarlo dos veces.
- Si se modifican las facturas después del pago, se muestra una advertencia
  para conciliar la diferencia; no se altera el pago ya registrado.
- Los perfiles de consulta pueden ver y descargar el informe, pero no registrar
  pagos. El PDF incluye las actividades, totales y datos del pago si existe.

El informe utiliza los datos y reglas del portal; la clasificación fiscal de
las facturas debe verificarse antes de declarar el impuesto.

## Ingresos y residentes

1. Tras iniciar sesión con permisos de administración, el formulario de **Nuevo Ingreso** se muestra desplegado al abrir cada
   residencia. Selecciona las fechas de nacimiento e ingreso con sus
   calendarios (formato `DD/MM/AAAA`). La edad se calcula al cambiar el nacimiento,
   según la fecha actual y si ya cumplió años, como `47 AÑOS`; no se edita
   manualmente. Pulsa **Guardar ingreso y generar planilla**.
   Los textos se convierten a MAYÚSCULAS al confirmar cada campo; el correo se
   convierte a minúsculas. El correo se escribe completo en un solo campo,
   sin selector de dominio adicional.
   Las cédulas se imprimen con prefijo V- o E- y puntos de miles; si son
   menores de diez millones se completan con un cero inicial. Sin prefijo se
   asume V. Los teléfonos venezolanos se imprimen como 0414-8248527.
   Los datos guardados y el PDF mantienen estas mismas reglas.
   **Ciudad** y **Estado** son desplegables con búsqueda, en ambas residencias.
   El catálogo local contiene 560 nombres de ciudades y localidades (incluidas
   capitales municipales), los 23 estados y el **Distrito Capital**.
   Vargas figura con su nombre actual, **La Guaira**. Si una localidad no está
   en el catálogo, puedes escribirla en Ciudad. Ambos campos son obligatorios,
   comienzan sin selección y conservan los valores del borrador.
   En las direcciones de la madre/tutora y del padre/tutor, el desplegable
   **La misma de la residente** usa la dirección actual de la estudiante y la
   actualiza si cambia. Esta dirección se guarda y se imprime en el PDF.
   Selecciona **Otra dirección** para escribir una dirección diferente.
2. El ingreso se guarda en una transacción con sus datos, recaudos, fotografía
   opcional y PDF completado. Al guardar se abre automáticamente **Residentes
   Activas**, sin filtros de búsqueda ni historial de bajas seleccionados, donde
   puedes consultar los datos y descargar la planilla aunque abras otra sesión.
   La descarga se realiza desde el registro de la residente, no desde Nuevo Ingreso.
   Una misma cédula no puede tener dos ingresos activos en la misma residencia.
   Ambas residencias usan el mismo formulario y funciones, con registros separados.
   La cédula de la madre es opcional y puede quedar vacía.
   La foto carnet cubre todo el interior de su recuadro, sin márgenes vacíos
   ni deformación. Si sus proporciones son diferentes, se recortan los bordes
   de forma centrada para ajustarla.
   Todos los datos añadidos al PDF usan Helvetica regular, sin negrita, con el
   tamaño de 10 puntos en todas las páginas. La dirección de la estudiante puede
   ocupar los dos renglones disponibles; el segundo comienza en el margen
   izquierdo, sin sangría. Los datos largos que no caben se incluyen
   completos en páginas de anexo, con una referencia numerada en su casilla,
   sin reducir el tamaño ni truncar el texto. El texto del modelo no cambia.
   **Eliminar planilla de ingreso** borra definitivamente el ingreso completo
   (registro, datos, foto y PDF), previa confirmación. Solo aparece en ingresos
   activos sin depósito, mensualidades ni servicios registrados, y esa condición se vuelve
   a comprobar al eliminar para proteger los pagos registrados desde otra sesión.
3. La primera mensualidad de un nuevo ingreso vence **el mismo día del ingreso**.
   Para guardar un nuevo ingreso también son obligatorias la **habitación**
   y la **cama** asignadas. Si falta alguna, se muestran los campos pendientes
   y no se genera la planilla ni se registra la residente. Los demás campos
   opcionales y los ingresos ya guardados no cambian.
   La primera mensualidad se cobra al ingresar. Los cobros siguientes comienzan
   el **día 1 de cada mes calendario**, independientemente del día del ingreso.
   El plazo mensual es del **1 al 5**; el retardo comienza el **día 6**.
   La alerta aparece al ingresar y desde el día 1 de cada mes, hasta registrar
   el pago; no se anticipa al mes siguiente.
   Para ingresos existentes, la adaptación se realiza una sola vez: si no hay
   mensualidades pagadas, se conserva el cobro inicial; si las hay, el próximo
   cobro pasa al día 1 del mes posterior al último vencimiento cubierto.
   Los pagos, recibos y bajas anteriores no se reescriben.
4. **Pagos Pendientes** muestra las residentes activas con esos avisos.
   **Residentes Activas** y **Residentes de Baja** permiten descargar su lista
   en PDF, también con acceso de solo consulta. El PDF respeta la búsqueda y
   corresponde únicamente a la residencia seleccionada, con numeración consecutiva.
   La lista de activas incluye fecha de ingreso, nombre y apellidos, cédula,
   celular, habitación y cama. La lista de bajas incluye fecha de ingreso,
   nombre y apellidos, cédula, fecha de egreso y motivos del egreso.
   En todos los cuadros de ambas residencias, en pantalla y en los PDF nuevos,
   los encabezados están centrados y en negrita. Las fechas, numeración, cédulas,
   teléfonos, habitaciones, camas, montos y tasas están centrados.
   Los nombres, conceptos, bancos, medios de pago y motivos del egreso se alinean
   a la izquierda, aunque contengan números. En las fichas de datos personales,
   la columna de valor se alinea según el tipo de dato de cada fila.
   El historial de gastos usa el mismo cuadro estático que los demás informes
   para respetar estas alineaciones. Los PDF ya guardados no se reescriben.
   En **Residentes Activas**, el nombre y la cédula se resaltan en rojo
   cuando hay un cobro pendiente desde su fecha de inicio,
   junto con el estado y la fecha de vencimiento, sin abrir la ficha.
   El resaltado se actualiza según el próximo vencimiento después de cada pago;
   no se aplica al historial de bajas.
   En **Residentes Activas**, los campos para registrar y confirmar cobros
   permanecen ocultos dentro de los botones **Recibir o confirmar depósito**
   y **Recibir o confirmar mensualidad**. Pulsa el botón correspondiente para
   desplegarlos. Abrir o cerrar estos paneles no registra pagos: siguen siendo
   obligatorias la confirmación de recepción y la acción de guardar.
   Las fechas de vencimiento, pagos registrados e historial siguen visibles.
   En **Pagos Pendientes** los formularios continúan desplegados.
   Después del pago de mensualidad, **Pago por otros Servicios** permite cobrar
   **De Nevera**, **De Aire Acondicionado** o **Llaves** desde un panel cerrado.
   Se ingresa el monto base en USD: en dólares se elige Efectivo, Binance o Zelle;
   en bolívares se indica banco receptor, tasa BCV y su fecha para convertir.
   Incluye número de factura manual y confirmación de recepción. No cobra mora
   ni cambia el vencimiento de la mensualidad. El pago y su recibo se guardan
   juntos, y el recibo queda disponible en el historial, incluso después de la baja.
   Usa el mismo formato de dos copias, con título **RECIBO DE PAGO POR OTROS SERVICIOS**
   y el concepto seleccionado. Comparte la serie correlativa de mensualidades,
   sin repetir números. Se permiten varios servicios el mismo día.
   Los servicios se incluyen en cobros del mes, bancos, dólares y acumulados.
   **Baja y entrega de depósito** permanece oculta en ambas vistas y se despliega
   al pulsar su botón. Abrirla no da de baja a la residente ni registra una
   devolución: siguen siendo necesarias las confirmaciones y **Dar de baja**.
   **Pago Recibido Deposito** registra la fecha del depósito único por llaves y
   depósito de habitación, en dólares (USD), previa confirmación de recepción.
   Es obligatorio ingresar el monto recibido (mayor que cero, con coma o punto
   decimal) e indicar si se recibió en efectivo, Binance o Zelle.
   Al guardar se genera un recibo PDF con un formato común para ambas residencias,
   número único por ingreso, nombre y cédula de la estudiante, fecha, concepto,
   monto en números y letras y forma de pago. El depósito y el recibo se guardan
   juntos: si falla la generación, no se guarda el depósito. El PDF se descarga
   desde la sección del depósito, incluso después de la baja. Las firmas quedan
   en blanco para firmar al imprimir. Se genera una sola hoja tamaño carta
   (612 × 792 puntos), con dos recibos: el primero sin rótulo de copia y el
   segundo identificado para el archivo de la residencia, separados por una
   línea intermitente central con una tijera pequeña, sin texto de recorte.
   El monto admite como máximo dos decimales.
   En ambos tipos de recibo, los títulos y etiquetas se imprimen en negrita;
   los datos insertados, incluidos concepto, monto y forma de pago, en letra normal.
   Los títulos son **RECIBO DE PAGO DE DEPOSITO** y **RECIBO DE PAGO DE MENSUALIDAD**.
   Ambas copias de estos recibos llevan al pie, centrada y en Helvetica regular
   de 7 puntos, la leyenda **NO ES VALIDO PARA FACTURA FISCAL**.
   Se aplica a los recibos generados a partir de este cambio; los PDF ya guardados
   no se modifican.
   Los recibos de mensualidad y otros servicios en bolívares desglosan **SUBTOTAL**, **IVA (16%)**
   y **TOTAL**, en Bs. El IVA se calcula como el 16% del pago completo recibido,
   redondeado a dos decimales; el subtotal es el total menos ese IVA.
   Por ejemplo: Bs. 160.000,00 recibidos = subtotal Bs. 134.400,00 +
   IVA Bs. 25.600,00. No se divide entre 1,16 ni se agrega un recargo al pago.
   El monto guardado y los acumulados conservan el pago completo.
   **Resumen de cobros**, en pantalla y en la lista mensual PDF, incluye
   **IVA (16%) (Bs.)** para cada mensualidad o servicio en bolívares, con el mismo cálculo
   y redondeo del recibo y formato `28.384,16`. Los depósitos, pagos en dólares
   y montos incompletos muestran «—». El IVA ya forma parte del pago recibido:
   no se suma de nuevo a los totales ni a los acumulados.
   Una fila **TOTAL** cierra el resumen de cobros en pantalla y PDF, sumando
   por separado bolívares, IVA y dólares del mes. El IVA total es la suma
   de los importes ya redondeados de cada mensualidad y servicio; no incluye depósitos
   ni se agrega nuevamente al monto recibido. No incluye saldos iniciales
   ni pagos de meses anteriores; los pagos incompletos conservan su advertencia.
   En **Pagos recibidos en el mes**, todos los montos en bolívares usan también
   punto de miles y coma decimal (`130.456,98`): cobros, IVA, saldos iniciales,
   totales, acumulados y resumen por banco, en pantalla y en el PDF.
   La tasa BCV usa los mismos separadores y conserva sus cuatro decimales.
   El IVA no se aplica a depósitos, devoluciones ni pagos en dólares.
   Los montos en bolívares y dólares de los recibos usan punto como separador de miles
   y coma decimal, siempre con dos decimales: **Bs. 28.384,16** y **USD 1.234,56**. Esta regla
   incluye mensualidad, subtotal, IVA y total, sin cambiar los valores guardados
   ni la precisión de la tasa BCV. Los recibos ya guardados no se reescriben
   automáticamente.
   Al registrar una mensualidad, el campo **Número de factura** permite ingresar
   manualmente hasta 40 caracteres, conservando ceros iniciales y guiones.
   Se guarda junto con el pago y su PDF, y se imprime como **FACTURA Nro.**
   en ambas copias del recibo de mensualidad, separado del número automático
   de recibo. Es opcional para conservar el registro de pagos sin factura.
   También aparece en la lista mensual. Las correcciones posteriores desde esa
   lista actualizan el reporte, pero no reescriben el recibo ya emitido.
   Todos los recibos de Carmen Luisa, incluida la entrega de depósito, llevan
   **PENSIÓN QUINTA CARMEN LUISA, C.A** y debajo **RIF: 504912700**.
   Todos los recibos de Las Acacias llevan **RESIDENCIAS LAS ACACIAS, C.A**
   y debajo **RIF: 50793608**. Para los mismos datos, todos los recibos y constancias
   tienen idéntico formato, textos, tipografía y distribución; solo cambian la
   razón social y el RIF. No se incluyen logos ni direcciones exclusivos de una residencia.
   La numeración es correlativa desde `00001`, con series independientes por tipo
   de recibo y residencia. El 1 de enero comienza una nueva serie anual según
   la fecha de emisión en la aplicación, aunque se registre un pago anterior.
   Se muestra como `00001-2026` para distinguir el año. Los números se asignan
   al guardar, dentro de la misma transacción del pago; un fallo no consume
   números. Descargar o regenerar un recibo conserva su número original.
   Los depósitos antiguos conservan su fecha y muestran «No registrado» donde
   no se hubiera guardado el monto, la moneda o la forma de pago.
   Se guarda una sola vez por ingreso, se conserva tras la baja y no modifica
   el próximo cobro ni cubre una mensualidad. El aviso del depósito indica que
   el monto será reembolsado al retirarse de la residencia, en el mismo tipo de
   moneda recibido, y que algunas condiciones aplican. Este aviso no registra
   ni ejecuta un reembolso. Los pagos mensuales se registran
   por separado; los pagos anteriores no se reclasifican como depósitos.
   **Registrar pago** requiere confirmar la recepción, seleccionar Bolívares o
   Dólares y cubre un solo período, avanzando el próximo cobro al día 1 del mes
   siguiente al período pagado, no desde el día de pago. Para Bolívares se ingresa el monto
   de la mensualidad en dólares y se indica manualmente la tasa BCV y su fecha.
   Se muestra el total a recibir en bolívares (dólares × tasa), redondeado a dos
   decimales, con los medios céntimos hacia arriba. Se guardan el monto base en
   dólares y el total recibido en bolívares; ambos aparecen en el recibo.
   Para
   Dólares se elige efectivo, Binance o Zelle. Si hay varios períodos atrasados,
   registra cada pago por separado. El historial conserva la fecha de registro,
   el vencimiento cubierto y los datos de monto, moneda, tasa y forma de pago.
   Al pulsar **Registrar pago** se genera y guarda un recibo PDF de mensualidad
   con los datos de la estudiante, número único, el concepto «Pago de Mensualidad
   de Habitación», fecha del pago, monto en números y letras y moneda.
   El vencimiento cubierto se conserva en el historial, no en el concepto del PDF.
   Para dólares incluye efectivo,
   Binance o Zelle; para bolívares incluye la tasa BCV y su fecha ingresadas.
   El formato es una hoja carta con dos copias y línea intermitente con tijera.
   Cada recibo se descarga desde el historial, incluso después de la baja.
   El historial muestra cada pago de depósito o mensualidad por separado, con
   sus datos y el botón de descarga del recibo inmediatamente debajo.
   Si se registra desde **Pagos Pendientes**, aparece además **Descargar recibo
   de pago de mensualidad (PDF)** para el último pago de la sesión, aunque la
   residente deje de aparecer en pendientes. La descarga permanente sigue
   disponible en el historial de la residente.
   Si falla la generación o el guardado, no se registra el pago ni se modifica
   el próximo vencimiento. El monto admite como máximo dos decimales.
   Los pagos anteriores a esta función conservan su historial sin PDF.
   **Pagos Pendientes** incluye las residentes activas al ingresar y desde el
   día 1 del mes pendiente, durante todo el plazo y el atraso, sin quitarlas
   de **Residentes Activas**. Al cubrir un período se vuelve a evaluar el próximo
   cobro: si todavía hay otro mes pendiente, sigue en pendientes.
   Los días de retardo mensual se cuentan desde el día 6 hasta la fecha del pago.
   Para el cobro inicial en una fecha distinta del día 1, se cuentan desde el
   día siguiente al ingreso. La mora no se calcula automáticamente: se ingresa manualmente
   en USD al registrar un pago atrasado (cero si no se cobra recargo).
   Se muestran mensualidad, mora y total antes de confirmar. En bolívares se
   convierte el total en USD (mensualidad más mora) con la tasa BCV manual y
   redondeo habitual. El recibo de un pago atrasado y el historial conservan
   la mensualidad, los días de retardo, la mora y el total recibido.
   No se agrega mora retroactiva a pagos ya registrados.
   Cada nueva mensualidad en bolívares requiere elegir el banco receptor:
   **Banco de Venezuela**, **Banco del Tesoro** o **Fondo Común**. Se conserva
   en el historial y se imprime en ambas copias del recibo. Los pagos anteriores
   sin banco guardado muestran «No registrado»; no se asigna un banco supuesto.
   El botón **Pagos recibidos en el mes**, independiente para cada residencia,
   lista un renglón por cobro del mes actual, incluyendo mensualidades y depósitos
   de ingreso, aunque la residente ingresara antes o ya esté de baja.
   En **Resumen de cobros** muestra fecha, nombre, cédula, habitación, cama,
   tipo de pago, monto en Bs., tasa BCV y fecha de la tasa, banco receptor,
   monto en USD y forma de pago (Zelle, Binance o efectivo). **Totales y acumulados**
   presenta los montos mensuales y acumulados por moneda.
   Si un pago anterior en bolívares no tiene fecha de la tasa BCV, muestra
   «No registrada» sin impedir consultar los cobros, totales ni descargar el PDF.
   La ausencia de tasa, banco, forma de pago, habitación, cama o factura tampoco
   bloquea la lista: cada dato faltante se señala como no registrado o sin indicar.
   Si falta el identificador interno, se avisa y solo se deshabilita la edición
   de la factura de ese pago; no se oculta ni se excluye de los totales.
   Los totales en Bs. y USD se calculan por separado con decimales exactos,
   sin sumar de nuevo la base en dólares de un pago recibido en bolívares.
   El resumen distingue acumulado de meses anteriores, cobros del mes y acumulado
   al cierre. En pantalla y en el PDF, **Resumen de Bolivares en Bancos** muestra
   Banco de Venezuela, Banco del Tesoro y Fondo Común, incluso si tienen cero:
   saldo inicial / acumulado anterior, recibido en el mes y acumulado
   al cierre, todos en Bs. La primera columna reúne los cobros de meses anteriores
   y todos los saldos de arranque registrados hasta el mes consultado, incluidos
   los cargados en ese mes; no incluye los cobros del mes.
   Primera columna + recibido en el mes = acumulado al cierre.
   Los saldos iniciales en bolívares se asignan a su banco
   una sola vez; los dólares no se suman en este desglose. Los cobros antiguos
   sin banco aparecen separados como «No registrado», sin atribuirlos a un banco.
   Este desglose no representa saldos bancarios disponibles: no resta gastos
   ni devoluciones. Al cambiar de mes (también de año), el acumulado previo pasa al
   siguiente automáticamente según las fechas guardadas, sin duplicar registros.
   Es un acumulado de saldos iniciales y cobros, no dinero disponible:
   no resta devoluciones ni gastos.
   Los saldos iniciales se guardan por residencia, moneda y medio (efectivo, Zelle,
   Binance o banco, según moneda), con fecha y descripción. Se muestran separados
   en el resumen y el PDF, sin crear pagos de residentes ni recibos ficticios.
   El acumulado anterior y el cierre incluyen esos saldos una sola vez, también
   al cambiar de año. El total recibido del mes solo incluye pagos de residentes.
   **Resumen de dólares Recibidos**, en pantalla y PDF, muestra Efectivo,
   Zelle y Binance (también cuando están en cero), con saldo inicial / acumulado
   anterior, cobros del mes y acumulado al cierre, usando la misma regla.
   Incluye depósitos y
   mensualidades recibidos realmente en USD, no la conversión de pagos en Bs.
   Los cobros sin medio registrado se separan como «No registrado». Los saldos
   iniciales no se presentan como pagos de residentes ni se suman dos veces.
   **Descargar lista de pagos del mes (PDF)** entrega la misma lista y resumen,
   con identificación fiscal de la residencia, mes, advertencias y páginas numeradas.
   Usa **Mes del informe** para volver a generar una lista de cualquier mes con
   pagos registrados, incluidos los informes creados antes de las correcciones.
   Descarga el PDF nuevo para reemplazar en tus archivos la copia anterior.
   Se imprime en hojas carta horizontales; las listas largas usan varias páginas.
   Cada fila se mantiene completa en una hoja, con encabezados repetidos cuando
   una tabla continúa. Los títulos de resumen acompañan sus datos. Se reservan
   márgenes de impresión y espacio exclusivo para la firma del administrador
   en la última página, sin superponerla a tablas ni al pie de página.
   La tabla usa líneas verticales continuas y uniformes, sin remarcar las
   divisiones entre Fecha/Residente, Bolívares/IVA y Tasa BCV/Fecha de la tasa.
   Los PDFs descargados anteriormente deben descargarse de nuevo desde el portal
   para ver el diseño corregido.
   Si una fila excepcionalmente larga no cabe completa en una hoja, se informa
   el error en lugar de generar un PDF con datos cortados.
   En **Ingresar o corregir números de factura** se escribe y guarda manualmente
   el número de factura entregada, por cada mensualidad o depósito del mes.
   La columna **N.º de factura** aparece en pantalla y en el PDF. Es independiente
   de la numeración automática del recibo; admite hasta 40 caracteres, conserva
   ceros iniciales y permite correcciones o dejarlo vacío para quitarlo.
   En la columna **Residente**, cada nombre ocupa como máximo dos líneas:
   primer nombre e inicial del segundo en la primera; primer apellido e inicial
   del segundo apellido en la segunda. El nombre completo permanece guardado.
   Los datos de **Habitación**, **Cama** y **N.º de factura** aparecen centrados
   en la tabla y en el PDF.
   Los pagos sin factura guardada muestran «Sin indicar». Los montos y recibos no cambian.
   Cada página indica «Fecha de impresión» con la fecha de generación de la descarga
   (no la fecha de impresión física). La última página reserva una línea para la
   firma manuscrita del administrador, sin superponerla a la lista o los totales.
   Los registros antiguos sin monto o moneda válidos no se suman y generan un aviso.
5. **Dar de baja** requiere motivo y confirmación. Conserva los datos, el PDF y
   el historial de pagos. Se consultan con el botón **Residentes de Baja**
   de cada residencia (también se conserva **Ver historial de bajas**); ya no
   aparecen en los avisos de cobro. Un reingreso crea un registro independiente.
   Al guardar una baja se abre automáticamente **Residentes de Baja**, sin filtros
   de búsqueda previos, tanto con novedades como sin novedades. El registro recién
   dado de baja queda abierto, con la constancia al principio para descargarla.
   Las bajas se ordenan de más reciente a más antigua; junto a la descarga se
   muestran número de constancia, depósito, descuento y reembolso para distinguir
   ingresos anteriores de la misma residente.
   Cuando existe devolución registrada, se muestra el botón
   **Descargar constancia de entrega de depósito (PDF)** en negrita.
   Si hay depósito, se verifican la entrega de llaves y el estado de la habitación,
   y se confirma la devolución en USD, seleccionando efectivo, Binance o Zelle.
   Al marcar llaves entregadas y habitación SIN NOVEDAD, los campos de descuento
   y motivo desaparecen de inmediato y se devuelve el depósito completo, con
   descuento cero, sin aplicar montos o motivos escritos antes de marcar ambas opciones.
   Si faltan llaves o hay novedades, es obligatorio indicar el descuento fijado
   por la administración y su motivo manual. El descuento se ingresa manualmente;
   si no hay descuento, se indica `00,00` (valor inicial del campo).
   El descuento admite dos decimales
   y no puede superar el depósito; puede descontarse el total (reembolso cero).
   La aplicación calcula depósito menos descuento y guarda el detalle junto con
   la **CONSTANCIA DE ENTREGA DE DEPOSITO**, concepto **ENTREGA DE DEPOSITO**.
   Incluye los datos de la estudiante, depósito original, descuento y motivo,
   total reembolsado en números y letras y las verificaciones realizadas.
   Mantiene una hoja carta con dos copias, línea intermitente y tijera,
   etiquetas en negrita y valores en letra normal. Tiene una serie propia
   por residencia, desde `00001` cada 1 de enero, según el año de emisión.
   La baja, devolución, PDF y número se guardan en una sola transacción:
   si algo falla, no se guarda ninguno. El PDF queda disponible en el historial
   de bajas sin volver a registrar una devolución al descargar.
   Sin depósito registrado se permite la baja, con aviso, sin devolución ni PDF
   de entrega. Si existe un depósito antiguo sin monto o moneda completos,
   se bloquea la baja con un error explícito para no inventar el reembolso.
   Las bajas anteriores no generan devoluciones retroactivas automáticamente.
   El registro documenta una entrega confirmada por la administración:
   la aplicación no realiza transferencias bancarias.

Las alertas visuales funcionan sin configurar correo. Los recordatorios externos
requieren la configuración y el proceso independiente descritos a continuación.
La vista **Pagos Pendientes** no solicita configurar credenciales de correo
ni muestra avisos por su ausencia; esa configuración puede realizarse más adelante.
Recarga la vista para actualizar fechas si quedó abierta al cambiar de día.
Los datos de un formulario sin guardar no son un ingreso permanente: deben
enviarse con el botón de guardado. Los registros creados antes de existir esta
base de datos no se recuperan automáticamente.

## Recordatorios de mensualidad por correo

El proceso [payment_reminders.py](./payment_reminders.py) envía un recordatorio
diario a las residentes activas con un cobro pendiente desde el ingreso o desde
el día 1 del mes. Usa el correo guardado en el ingreso, con el nombre y la
razón social correspondientes a cada residencia. Para fechas pasadas dice
«venció el» y durante los días 1 al 5 indica el plazo mensual de pago.
Incluye «EVITA PAGOS POR MORA. ¡Muchas gracias!». No adjunta datos adicionales,
planillas ni recibos; cada correo tiene una sola destinataria.

Se revisan ambas residencias desde las **08:00, hora de Venezuela**
(`America/Caracas`). Se registra un máximo de un intento por ingreso y día,
incluso al reiniciar el proceso, ejecutar dos procesos simultáneos o pagar un
período y seguir teniendo otro vencido. Se comprueba el próximo vencimiento
antes de reservar el envío; si cambió o la residente fue dada de baja, no se
reserva ese recordatorio. Después de un pago, la siguiente revisión usa el
nuevo vencimiento. Las residentes de baja y las que aún no tienen un cobro pendiente
no reciben avisos.

### Configurar Gmail sin publicar credenciales

El remitente predeterminado de ambas residencias es `farfanlirio16@gmail.com`.
Activa la verificación en dos pasos en esa cuenta y crea una **contraseña de
aplicación de Google**. No uses la contraseña normal ni la escribas en el chat,
el código o Git. Configúrala como secreto del Codespace llamado
`REMINDER_SMTP_PASSWORD`; si es necesario cambiar el remitente, usa
`REMINDER_SMTP_USER` con otra dirección Gmail. Reinicia el Codespace para que los
procesos reciban los secretos nuevos. El envío usa `smtp.gmail.com:465` con TLS
y validación del certificado; no requiere paquetes adicionales.

Simulación segura, **sin enviar correos ni registrar intentos**:

```sh
python payment_reminders.py
```

Después de configurar el secreto, ejecutar el proceso independiente:

```sh
python payment_reminders.py --send --watch
```

Este comando **sí envía correos reales**. Debe permanecer en ejecución junto
a Streamlit; no basta con abrir el portal. Revisa cada minuto después de las
08:00 y permite avisar a un ingreso añadido durante el día. Si el Codespace
se enciende después de las 08:00, procesa solo el día actual, sin acumular avisos
de días anteriores. Si se suspende o se cierra, **no se envían correos**; para
garantizar el horario se necesita un servidor permanentemente encendido.
Los procesos deben reiniciarse al reiniciar el Codespace. También puede usarse
`python payment_reminders.py --send` para una sola revisión (envío real).

**Pagos Pendientes** muestra si falta configurar Gmail y el número de correos
aceptados por Gmail hoy por residencia. También advierte sobre intentos fallidos
o sin confirmar. Aceptado por Gmail no garantiza la entrega: revisa rebotes y spam.
El registro SQLite conserva el día, vencimiento y estado del intento, sin
contraseñas ni cuerpos de correo. Los errores se registran con identificadores
internos, sin direcciones ni credenciales. Si el correo es inválido o falla SMTP,
no se informa éxito ni se modifica el pago. Si se interrumpe el proceso durante
el envío, queda pendiente/sin confirmar; **no se reintenta ese día** porque Gmail
podría haberlo aceptado. Corrige la configuración o dirección y revisa Gmail
antes de considerar otro envío; al día siguiente vuelve a ser elegible si sigue
pendiente. Esta precaución evita duplicados, pero puede omitir un aviso tras un
fallo: no se puede garantizar entrega exactamente una vez mediante SMTP.

## Almacenamiento y privacidad

La base SQLite se crea en `data/residencias.sqlite3` y **no se publica en Git**.
Los datos guardados sobreviven a recargas, nuevas sesiones y reinicios del
proceso en el mismo disco. No sobreviven a eliminar el Codespace o perder su
disco: usa almacenamiento persistente y copias de seguridad para uso real.
Para una copia consistente, detén la aplicación y respalda la base completa.
No subas bases, fotos ni planillas completadas con datos personales a GitHub.

La aplicación aún no tiene autenticación ni roles: mantén privado el puerto del
Codespace y limita el acceso a personal autorizado. No expongas esta versión
como servicio público con datos personales.

## Fuente del catálogo geográfico

[venezuela_locations.json](./venezuela_locations.json) deriva de
[zokeber/venezuela-json](https://github.com/zokeber/venezuela-json/tree/b56efbba1d57da5e5cc3815c50b9b6e4afffa534),
basado en marydn/venezuela-sql, bajo licencia
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Se combinaron las ciudades y las capitales estatales y municipales, se eliminaron
duplicados y se actualizó el nombre del estado Vargas a La Guaira.
No requiere conexión a Internet al usar el formulario; el catálogo no garantiza
cubrir todos los asentamientos y permite introducir localidades adicionales.

## Pruebas

```sh
python -m unittest discover -s tests -v
```

Las pruebas usan bases temporales y datos ficticios. Comprueban persistencia,
aislamiento de residencias, PDF, meses calendario, alertas, duplicados, pagos,
bajas y navegación de Streamlit.
