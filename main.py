from flask import Flask, render_template, request, redirect, flash
import os

try:
    import ifcopenshell
except ImportError:
    ifcopenshell = None

UPLOAD_FOLDER = 'uploads'

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Needed for flash messages
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024  # 300MB max

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        flash('Keine Datei ausgewählt.')
        return redirect('/')
    
    file = request.files['file']
    
    if file.filename == '':
        flash('Dateiname ist leer.')
        return redirect('/')
    
    if file and file.filename.lower().endswith('.ifc'):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)
        
        # Try to open with ifcopenshell
    if ifcopenshell:
        model = ifcopenshell.open(filepath)
        walls = model.by_type("IfcWall")

        print(f"Gefundene IfcWall Elemente: {len(walls)}")

        if walls:
            first_wall = walls[0]
            print("\n🧠 Verfügbare Attribute für IfcWall:")
            for attr_name in first_wall.get_info().keys():
                print(f"  - {attr_name}")


        flash('Datei erfolgreich hochgeladen und gelesen.')
        return redirect('/')
    else:
        flash('Nur .ifc Dateien sind erlaubt.')
        return redirect('/')
    
if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(debug=True)
