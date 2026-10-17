nombre = st.text_input("Ingrese su nombre:")
if st.button("Saludar"):
    st.success(f"¡Bienvenido, {nombre}!")
