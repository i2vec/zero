# Molecular Dynamics Simulation of Amorphous SiO2

## Problem background
Amorphous SiO₂ (silica) nanoparticles are widely used in ceramics, polymers, and construction materials, but their atomic structure is poorly understood and lacks a crystallographic reference. Without a reference model, identifying the structural state of amorphous silica nanoparticles from X‑ray diffraction data is difficult. Molecular dynamics (MD) simulations can generate an optimized amorphous structural model whose density, energy, and lattice parameters can serve as a reproducible fingerprint for identifying and comparing real amorphous SiO₂ nanoparticles.

## Approach
A periodic triclinic cell containing 144 atoms (48 Si, 96 O) is built starting from a crystalline α‑quartz structure. The atomic interactions are described by a universal force field (UFF) that includes Coulombic and Lennard‑Jones pair potentials. A microcanonical (NVE) MD simulation is run at 300 K with a 1 fs time step and periodic boundary conditions. The total potential energy is monitored while the system density is varied over a range. The density that yields the lowest total energy is identified as the optimized amorphous state. At this optimal density, the simulation cell is relaxed and its triclinic lattice parameters (a, b, c, α, β, γ) are extracted.

## Reproduction target
Run the described MD simulation and find the atomic density (in g/cm³) that minimizes the total potential energy. Report that optimal density, the corresponding total energy (kcal/mol), and the final triclinic cell's lattice parameters a, b, c (nm) and angles α, β, γ (degrees) in a CSV file named optimization_results.csv.

## Assets

- Crystallography Open Database (COD) - card 96-901-3493 (SiO2): https://www.crystallography.net/cod/
- LAMMPS molecular dynamics simulator: https://www.lammps.org/
- Universal Force Field (UFF) parameters for Si and O: 10.1021/ja00051a040

## Workflow steps

### Step 1: Retrieve initial crystalline SiO2 structure
- Role: process
- Action: Download the CIF file for COD card 96-901-3493 (SiO2) and convert it to a LAMMPS data file. This provides the starting atomic configuration (144 atoms: 48 Si, 96 O) for the MD simulation.
- Evidence: `/app/outputs/initial_structure.log`

### Step 2: MD amorphisation and energy minimisation
- Role: scored (load-bearing)
- Action: Run a LAMMPS molecular dynamics simulation of the periodic SiO2 system using a universal force field (Coulomb + Lennard-Jones, with electrostatic cutoff 1.85 nm and van der Waals force evaluation accuracy 10^-5 kcal/mol). Perform NVE dynamics at 300 K with a time step of 1 fs for 1000 iterations, applying periodic boundary conditions. Vary the atomic density from 1.5 to 3.0 g/cm^3 to find the density that minimizes total potential energy. At the optimal density, perform a final relaxation and extract the lattice parameters (a, b, c, alpha, beta, gamma) assuming a triclinic cell (space group P1). Write the result to optimization_results.csv with columns: density (g/cm^3), total_energy (kcal/mol), a (nm), b (nm), c (nm), alpha (deg), beta (deg), gamma (deg).
- Output file: `/app/outputs/optimization_results.csv`
- Format: csv
- Contract: density (float, g/cm^3), total_energy (float, kcal/mol), a (float, nm), b (float, nm), c (float, nm), alpha (float, deg), beta (float, deg), gamma (float, deg)
- Scoring: scored by hidden verifier

## Output files
Write all artifacts under `/app/outputs`:
- `/app/outputs/optimization_results.csv`

## Output contract

Every file the hidden verifier reads is described below. Write each file under `/app/outputs` and follow its schema exactly.

### optimization_results.csv
- path: `/app/outputs/optimization_results.csv`
- format: csv
- purpose: scored
- target_policy: reference_match
- description: The agent's computed optimal density, total energy, and triclinic lattice parameters of the amorphous SiO2 domain.
- schema:
  - `type`: table
  - `required_columns`: `density`, `total_energy`, `a`, `b`, `c`, `alpha`, `beta`, `gamma`
  - `units`:
    - `density`: g/cm^3
    - `total_energy`: kcal/mol
    - `a`: nm
    - `b`: nm
    - `c`: nm
    - `alpha`: deg
    - `beta`: deg
    - `gamma`: deg

Notes: The checker compares the reported values to hidden reference values with tolerances.

## Self-check before finishing (optional, not scored)

A machine-readable copy of the output contract is below. Before you finish, write and run a small script that checks every file under `/app/outputs` against it: each declared file exists, JSON objects contain the required keys, and CSV/TSV files contain the required columns. Fix any mismatch before finishing.

This checks SHAPE ONLY (files, keys, columns) — it does NOT judge scientific correctness, and passing it does not mean your answer is correct.

```json
{
  "outputs": [
    {
      "file": "optimization_results.csv",
      "format": "csv",
      "purpose": "scored",
      "target_policy": "reference_match",
      "schema": {
        "type": "table",
        "required_columns": [
          "density",
          "total_energy",
          "a",
          "b",
          "c",
          "alpha",
          "beta",
          "gamma"
        ],
        "units": {
          "density": "g/cm^3",
          "total_energy": "kcal/mol",
          "a": "nm",
          "b": "nm",
          "c": "nm",
          "alpha": "deg",
          "beta": "deg",
          "gamma": "deg"
        }
      },
      "description": "The agent's computed optimal density, total energy, and triclinic lattice parameters of the amorphous SiO2 domain."
    }
  ],
  "notes": "The checker compares the reported values to hidden reference values with tolerances."
}
```

## How you are scored
A hidden verifier reads your optimization_results.csv and compares each reported value to hidden reference values within predefined tolerances. Each field is scored independently: density, total energy, and each lattice parameter are checked; the reward is the weighted sum of these components. The verifier expects the numbers to result from an actual MD simulation following the stated protocol, not from guessing or copying the paper's reported values.
