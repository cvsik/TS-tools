from rdkit import Chem
from rdkit.Chem import AllChem
import numpy as np
import autode as ade
import os
from autode.conformers import conf_gen
from autode.conformers import conf_gen, Conformer
from scipy.spatial import distance_matrix
import copy
import subprocess
from itertools import combinations
import re

ps = Chem.SmilesParserParams()
ps.removeHs = False
bohr_ang = 0.52917721090380

xtb = ade.methods.XTB()


def find_ts_guess(reactant_smiles, product_smiles, solvent=None, n_conf=5, fc_min=0.02, fc_max=0.9, fc_delta=0.05):
    """
    Finds a transition state (TS) guess based on the given reactant and product SMILES strings.

    Args:
        reactant_smiles (str): SMILES string of the reactant.
        product_smiles (str): SMILES string of the product.
        solvent (str, optional): Solvent to consider during calculations. Defaults to None.
        n_conf (int, optional): Number of additional conformers to generate. Defaults to 5.
        fc_min (float, optional): Minimum force constant value. Defaults to 0.02.
        fc_max (float, optional): Maximum force constant value. Defaults to 0.2.
        fc_delta (float, optional): Increment value for force constant. Defaults to 0.02.

    Returns:
        None
    """
    # Get the reactant and product mol
    full_reactant_mol = Chem.MolFromSmiles(reactant_smiles, ps)
    full_product_mol = Chem.MolFromSmiles(product_smiles, ps)

    charge = Chem.GetFormalCharge(full_reactant_mol)

    formed_bonds, broken_bonds = get_active_bonds(full_reactant_mol, full_product_mol) 

    # Construct dicts to translate between map numbers idxs and vice versa
    full_reactant_dict = {atom.GetAtomMapNum(): atom.GetIdx() for atom in full_reactant_mol.GetAtoms()}

    # Get the constraints for the initial FF conformer search
    formation_constraints = get_optimal_distances(product_smiles, full_reactant_dict, formed_bonds, solvent=solvent, charge=charge)
    breaking_constraints = get_optimal_distances(reactant_smiles, full_reactant_dict, broken_bonds, solvent=solvent, charge=charge)
    formation_constraints_stretched = formation_constraints.copy()
    formation_constraints_stretched.update((x, 2.0 * y) for x, y in formation_constraints_stretched.items())

    # Combine constraints if multiple reactants
    constraints = breaking_constraints.copy()
    if len(reactant_smiles.split('.')) != 1:
        for key, val in formation_constraints_stretched.items():
            if key not in constraints:
                constraints[key] = val

    # Generate initial optimized conformer with the correct stereochemistry
    optimized_ade_reactant_mol = optimize_molecule_with_extra_constraints(
        full_reactant_mol,
        reactant_smiles,
        constraints,
        charge
    )

    # generate a geometry for the product and save xyz
    breaking_constraints_stretched = breaking_constraints.copy()
    breaking_constraints_stretched.update((x, 2.0 * y) for x, y in breaking_constraints_stretched.items())
    product_constraints = formation_constraints.copy()
    if len(product_smiles.split('.')) != 1:
        for key,val in breaking_constraints_stretched.items():
            if key not in product_constraints:
                product_constraints[key] = val
    _ = optimize_molecule_with_extra_constraints(
        full_product_mol,
        product_smiles,
        product_constraints,
        charge,
        name='product'
    )

    # Generate additional conformers for the reactants
    conformers_to_do = generate_additional_conformers(
        optimized_ade_reactant_mol,
        full_reactant_mol,
        constraints,
        charge,
        solvent,
        n_conf
    )
    
    # Apply attractive potentials and run optimization
    for conformer in conformers_to_do:
        preliminary_ts_guess_index = None
        # Find the minimal force constant yielding the product upon application of the formation constraints and
        # use that one to generate a first TS guess
        for force_constant in np.arange(fc_min, fc_max, fc_delta):
            energies, coords, atoms, potentials = get_profile_for_biased_optimization(
                conformer,
                formation_constraints,
                force_constant,
                charge=charge,
                solvent=solvent
            )
            #print(energies, coords, atoms, potentials)
            if potentials[-1] > 0.01:
                continue  # Means that you haven't reached the products
            else:
                preliminary_ts_guess_index = get_ts_guess_index(energies, potentials)
                break

        # TODO: try to replace this by an additional optimization wherein you fix the active bond length if more than one imaginary frequency
        # If the preliminary TS guess has multiple imaginary frequencies, iterate through the next 6 optimization points
        # and select the first point that yields only 1 imaginary frequency (if all have multiple imaginary frequencies,
        # then return the preliminary guess)
        if preliminary_ts_guess_index is not None:
            xyz_file_final_ts_guess = get_final_ts_guess_geometry(
                preliminary_ts_guess_index,
                atoms,
                coords,
                force_constant,
                charge
            )
        else:
            print(potentials[-1])
            print('No TS guess found')
            return None  # Means that no TS guess was found

        return xyz_file_final_ts_guess


def get_final_ts_guess_geometry(preliminary_ts_guess_index, atoms, coords, force_constant, charge):
    """
    Retrieves the final transition state (TS) guess geometry based on the given parameters.

    Args:
        preliminary_ts_guess_index (int): Index of the preliminary TS guess.
        atoms (list): List of atom objects.
        coords (list): List of coordinate objects.
        force_constant (float): Force constant value.
        charge (int): Charge value.

    Returns:
        str: Filename of the final TS guess geometry XYZ file.
    """
    for index in range(preliminary_ts_guess_index, min(preliminary_ts_guess_index + 6, len(atoms))):
        filename = write_xyz_file_from_atoms_and_coords(
            atoms[index],
            coords[index],
            f'ts_guess_{force_constant}.xyz'
        )
        neg_freq = get_negative_frequencies(filename, charge)

        if len(neg_freq) == 1:
            return filename
        filename = write_xyz_file_from_atoms_and_coords(
            atoms[index],
            coords[index],
            f'ts_guess_{force_constant}.xyz'
        )
        neg_freq = get_negative_frequencies(filename, charge)

        if index == preliminary_ts_guess_index:
            neg_freq_init = neg_freq 

        if len(neg_freq) == 1:
            print(f'These are the negative frequencies found for the TS guess: {neg_freq}')
            return filename 
    
    # If no exit yet, then return the geometry at the original index
    print(f'These are the negative frequencies found for the TS guess: {neg_freq_init}')
    filename = write_xyz_file_from_atoms_and_coords(
        atoms[preliminary_ts_guess_index],
        coords[preliminary_ts_guess_index],
        f'ts_guess_{force_constant}.xyz'
    )

    return filename


def get_negative_frequencies(filename, charge):
    """
    Executes an external program to calculate the negative frequencies for a given file.

    Args:
        filename (str): The name of the file to be processed.
        charge (int): The charge value for the calculation.

    Returns:
        list: A list of negative frequencies.
    """
    with open('hess.out', 'w') as out:
        process = subprocess.Popen(f'xtb {filename} --charge {charge} --hess'.split(), 
                                   stderr=subprocess.DEVNULL, stdout=out)
        process.wait()
    
    neg_freq = read_negative_frequencies('g98.out')
    return neg_freq


def get_ts_guess_index(energies, potentials):
    """
    Returns the index of the transition state (TS) guess based on the energies and potentials.

    Args:
        energies (list): List of energy values.
        potentials (list): List of potential values.

    Returns:
        int: Index of the TS guess.
    """
    true_energy = list(energies - potentials)
    ts_guess_index = true_energy.index(max(true_energy))

    return ts_guess_index


def get_profile_for_biased_optimization(conformer, formation_constraints, force_constant, charge, solvent):
    """
    Retrieves the profile for biased optimization based on the given parameters.

    Args:
        conformer: The conformer object.
        formation_constraints: Constraints for formation.
        force_constant: Force constant value.
        charge: Charge value.
        solvent: Solvent to consider.

    Returns:
        tuple: A tuple containing energies, coordinates, atoms, and potentials.
    """
    log_file = xtb_optimize_with_applied_potentials(conformer, formation_constraints, force_constant, charge=charge, solvent=solvent)
    energies, coords, atoms = read_energy_coords_file(log_file)
    potentials = determine_potential(coords, formation_constraints, force_constant)
    write_xyz_file_from_atoms_and_coords(atoms[-1], coords[-1], 'product_geometry_obtained.xyz')

    return energies, coords, atoms, potentials


def generate_additional_conformers(optimized_ade_mol, full_reactant_mol, constraints, charge, solvent, n_conf):
    """
    Generate additional conformers based on the optimized ADE molecule and constraints.

    Args:
        optimized_ade_mol: The optimized ADE molecule.
        full_reactant_mol: The full reactant molecule.
        constraints: Constraints for conformer generation.
        charge: Charge value.
        solvent: Solvent to consider.
        n_conf: Number of additional conformers to generate.

    Returns:
        list: A list of additional conformers.
    """
    conformer_xyz_files = []

    for n in range(n_conf):
        atoms = conf_gen.get_simanl_atoms(species=optimized_ade_mol, dist_consts=constraints, conf_n=n_conf)
        conformer = Conformer(name=f"conformer_{n}", atoms=atoms, charge=charge, dist_consts=constraints)
        write_xyz_file_from_ade_atoms(atoms, f'{conformer.name}.xyz')
        optimized_xyz = xtb_optimize(f'{conformer.name}.xyz', charge=charge, solvent=solvent)
        conformer_xyz_files.append(optimized_xyz)

    clusters = count_unique_conformers(conformer_xyz_files, full_reactant_mol)
    conformers_to_do = [conformer_xyz_files[cluster[0]] for cluster in clusters]

    return conformers_to_do


def optimize_molecule_with_extra_constraints(full_mol, smiles, constraints, charge, name='reactant'):
    """
    Optimize molecule with extra constraints.

    Args:
        full_mol: The full RDKIT molecule.
        smiles: SMILES representation of the molecule.
        constraints: Constraints for optimization.
        charge: Charge value.
        name: name to be used in generated xyz-files

    Returns:
        object: The optimized ADE molecule.
    """
    get_conformer(full_mol)
    write_xyz_file_from_mol(full_mol, f'input_{name}.xyz')

    ade_mol = ade.Molecule(f'input_{name}.xyz', charge=charge)
    for node in ade_mol.graph.nodes:
        ade_mol.graph.nodes[node]['stereo'] = False

    bonds = []
    for bond in full_mol.GetBonds():
        i, j = bond.GetBeginAtom().GetIdx(), bond.GetEndAtom().GetIdx()
        if (i, j) not in constraints and (j, i) not in constraints:
            bonds.append((i, j))

    ade_mol.graph.edges = bonds

    stereochemistry_smiles_reactants = get_stereochemistry_from_smiles(full_mol)

    for n in range(100):
        atoms = conf_gen.get_simanl_atoms(species=ade_mol, dist_consts=constraints, conf_n=n)
        conformer = Conformer(name=f"conformer_{name}_init", atoms=atoms, charge=charge, dist_consts=constraints)
        write_xyz_file_from_ade_atoms(atoms, f'{conformer.name}.xyz')
        embedded_mol, stereochemistry_xyz_reactants = get_stereochemistry_from_xyz(f'{conformer.name}.xyz', smiles)
        if stereochemistry_smiles_reactants == stereochemistry_xyz_reactants:
            break

    if len(stereochemistry_smiles_reactants) != 0:
        embedded_mol = assign_cis_trans_from_geometry(embedded_mol, smiles_with_stereo=smiles)
        write_xyz_file_from_mol(embedded_mol, f"conformer_{name}_init.xyz")

    ade_mol_optimized = ade.Molecule(f'conformer_{name}_init.xyz')

    return ade_mol_optimized


#TODO: maybe product mol too?
def get_stereochemistry_from_smiles(reactant_mol):
    """
    Check if the stereochemistry is present in the reactant molecule SMILES.

    Args:
        reactant_mol: The reactant molecule.

    Returns:
        list: The stereochemistry information.
    """
    stereochemistry = Chem.FindMolChiralCenters(reactant_mol)

    return stereochemistry


def find_cis_trans_elements(mol):
    """
    Find cis-trans elements in the molecule.

    Args:
        mol: The molecule.

    Returns:
        list: The cis-trans elements.
    """
    cis_trans_elements = []
    
    for bond in mol.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE:
            stereo = bond.GetStereo()
            if stereo == Chem.rdchem.BondStereo.STEREOZ or stereo == Chem.rdchem.BondStereo.STEREOE:
                cis_trans_elements.append((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), stereo))

    return cis_trans_elements


def add_xyz_conformer(smiles, xyz_file):
    """
    Add an XYZ conformer to the molecule.

    Args:
        smiles: The SMILES string.
        xyz_file: The XYZ file.

    Returns:
        object: The molecule with the added conformer.
    """
    mol = Chem.MolFromSmiles(smiles, ps)
    
    with open(xyz_file, 'r') as f:
        lines = f.readlines()
        num_atoms = int(lines[0])
        coords = []
        symbols = []
        for i in range(2, num_atoms+2):
            line = lines[i].split()
            symbol = line[0]
            x, y, z = map(float, line[1:])
            symbols.append(symbol)
            coords.append((x, y, z))

    conformer = Chem.Conformer(num_atoms)
    for i, coord in enumerate(coords):
        conformer.SetAtomPosition(i, coord)
    mol.AddConformer(conformer)
    
    return mol


def get_stereochemistry_from_xyz(xyz_file, smiles):
    """
    Get stereochemistry information from an XYZ file.

    Args:
        xyz_file: The XYZ file.
        smiles: The SMILES string.

    Returns:
        object: The molecule with stereochemistry.
        list: The stereochemistry information.
    """
    mol = Chem.MolFromSmiles(smiles, ps)
    Chem.RemoveStereochemistry(mol)
    no_stereo_smiles = Chem.MolToSmiles(mol)
    mol = add_xyz_conformer(no_stereo_smiles, xyz_file)

    mol.GetConformer()

    Chem.AssignStereochemistryFrom3D(mol)

    stereochemistry = Chem.FindMolChiralCenters(mol)

    return mol, stereochemistry


def extract_atom_map_numbers(string):
    """
    Extract atom map numbers from a string.

    Args:
        string: The input string.

    Returns:
        list: The extracted atom map numbers.
    """
    matches = re.findall(r'/\[[A-Za-z]+:(\d+)]', string)
    matches += re.findall(r'\\\[[A-Za-z]+:(\d+)]', string)
    
    return list(map(int, matches))

# TODO: This still seems to contain an error!!!
def assign_cis_trans_from_geometry(mol, smiles_with_stereo):
    """
    Assign cis-trans configuration to the molecule based on the geometry.

    Args:
        mol: The molecule.
        smiles_with_stereo: The SMILES string with stereochemistry information.

    Returns:
        object: The molecule with assigned cis-trans configuration.
    """
    cis_trans_elements = []
    mol_with_stereo = Chem.MolFromSmiles(smiles_with_stereo, ps)
    cis_trans_elements = find_cis_trans_elements(mol_with_stereo)
    involved_atoms = extract_atom_map_numbers(smiles_with_stereo) # aren't these just the atoms of the double bond (j,k)???

    for bond in mol.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE:
            atomj_idx = bond.GetBeginAtomIdx()
            atomk_idx = bond.GetEndAtomIdx()
            conf = mol.GetConformer()
            neighbors_atomj = mol.GetAtomWithIdx(atomj_idx).GetNeighbors()
            neighbors_atomk = mol.GetAtomWithIdx(atomk_idx).GetNeighbors()
            try:
                atomi_idx = [atom.GetIdx() for atom in neighbors_atomj if atom.GetAtomMapNum() in involved_atoms][0]
                atoml_idx = [atom.GetIdx() for atom in neighbors_atomk if atom.GetAtomMapNum() in involved_atoms][0]
            except IndexError:
                continue

            if (atomj_idx, atomk_idx, Chem.rdchem.BondStereo.STEREOZ) in cis_trans_elements:
                angle = 0
            elif (atomj_idx, atomk_idx, Chem.rdchem.BondStereo.STEREOE) in cis_trans_elements:
                angle = 180
            else:
                raise KeyError

            Chem.rdMolTransforms.SetDihedralDeg(conf, atomi_idx, atomj_idx, atomk_idx, atoml_idx, angle)

    return mol


def read_negative_frequencies(filename):
    """
    Read the negative frequencies from a file.

    Args:
        filename: The name of the file.

    Returns:
        list: The list of negative frequencies.
    """
    with open(filename, 'r') as file:
        for line in file:
            if line.strip().startswith('Frequencies --'):
                frequencies = line.strip().split()[2:]
                negative_frequencies = [freq for freq in frequencies if float(freq) < 0]
                return negative_frequencies


def write_xyz_file_from_ade_atoms(atoms, filename):
    """
    Write an XYZ file from the ADE atoms object.

    Args:
        atoms: The ADE atoms object.
        filename: The name of the XYZ file to write.
    """
    with open(filename, 'w') as f:
        f.write(str(len(atoms)) + '\n')
        f.write('Generated by write_xyz_file()\n')
        for atom in atoms:
            f.write(f'{atom.atomic_symbol} {atom.coord[0]:.6f} {atom.coord[1]:.6f} {atom.coord[2]:.6f}\n')


def write_xyz_file_from_atoms_and_coords(atoms, coords, filename='ts_guess.xyz'):
    """
    Write an XYZ file from a list of atoms and coordinates.

    Args:
        atoms: The list of atom symbols.
        coords: The list of atomic coordinates.
        filename: The name of the XYZ file to write (default: 'ts_guess.xyz').

    Returns:
        str: The name of the written XYZ file.
    """
    with open(filename, 'w') as f:
        f.write(f'{len(atoms)}\n')
        f.write("test \n")
        for i, coord in enumerate(coords):
            x, y, z = coord
            f.write(f"{atoms[i]} {x:.6f} {y:.6f} {z:.6f}\n")
    return filename


def xtb_optimize(xyz_file_path, charge=0, solvent=None):
    """
    Perform an xTB optimization of the geometry in the given XYZ file and return the path to the optimized geometry file.

    Args:
        xyz_file_path: The path to the XYZ file to optimize.
        charge: The charge of the molecule (default: 0).
        solvent: The solvent to consider during the optimization (default: None).

    Returns:
        str: The path to the optimized XYZ file.
    """
    if solvent is not None:
        cmd = f'xtb {xyz_file_path} --opt --charge {charge} --solvent {solvent}'
    else:
        cmd = f'xtb {xyz_file_path} --opt --charge {charge}'
    with open(os.path.splitext(xyz_file_path)[0] + '.out', 'w') as out:
        #print(cmd)
        process = subprocess.Popen(cmd.split(), stderr=subprocess.DEVNULL, stdout=out)
        process.wait()

    os.rename('xtbopt.xyz', f'{os.path.splitext(xyz_file_path)[0]}_optimized.xyz')

    return f'{os.path.splitext(xyz_file_path)[0]}_optimized.xyz'


def xtb_optimize_with_applied_potentials(xyz_file_path, constraints, force_constant, charge=0, solvent=None):
    """
    Perform an xTB optimization with applied potentials of the geometry in the given XYZ file and return the path to the log file.

    Args:
        xyz_file_path (str): The path to the XYZ file to optimize.
        constraints (dict): A dictionary specifying the atom index pairs and their corresponding distances.
        force_constant (float): The force constant to apply to the constraints.
        charge (int): The charge of the molecule (default: 0).
        solvent (str): The solvent to consider during the optimization (default: None).

    Returns:
        str: The path to the xTB log file.
    """
    xtb_input_path = os.path.splitext(xyz_file_path)[0] + '.inp'

    with open(xtb_input_path, 'w') as f:
        f.write('$constrain\n')
        f.write(f'    force constant={force_constant}\n')
        for key, val in constraints.items():
            f.write(f'    distance: {key[0] + 1}, {key[1] + 1}, {val}\n')
        f.write('$end\n')

    if solvent is not None:
        cmd = f'xtb {xyz_file_path} --opt --input {xtb_input_path} -v --charge {charge} --solvent {solvent}'
    else:
        cmd = f'xtb {xyz_file_path} --opt --input {xtb_input_path} -v --charge {charge}'

    with open(os.path.splitext(xyz_file_path)[0] + '_path.out', 'w') as out:
        #print(cmd)
        process = subprocess.Popen(cmd.split(), stderr=subprocess.DEVNULL, stdout=out)
        process.wait()

    os.rename('xtbopt.log', f'{os.path.splitext(xyz_file_path)[0]}_path.log')

    return f'{os.path.splitext(xyz_file_path)[0]}_path.log'


def count_unique_conformers(xyz_file_paths, full_reactant_mol):
    """
    Count the number of unique conformers among a list of XYZ file paths based on RMSD clustering.

    Args:
        xyz_file_paths (list): A list of paths to the XYZ files.
        full_reactant_mol (Chem.Mol): The full reactant molecule as an RDKit Mol object.

    Returns:
        list: A list of clusters, where each cluster contains the indices of conformers belonging to the same cluster.
    """
    molecules = []
    for xyz_file_path in xyz_file_paths:
        with open(xyz_file_path, 'r') as xyz_file:
            lines = xyz_file.readlines()
            num_atoms = int(lines[0])
            coords = [list(map(float, line.split()[1:])) for line in lines[2:num_atoms+2]]
            mol = Chem.Mol(full_reactant_mol)
            conformer = mol.GetConformer()
            for i in range(num_atoms):
                conformer.SetAtomPosition(i, coords[i])
            molecules.append(mol)

    rmsd_matrix = np.zeros((len(molecules), len(molecules)))
    for i, j in combinations(range(len(molecules)), 2):
        rmsd = AllChem.GetBestRMS(molecules[i], molecules[j])
        rmsd_matrix[i, j] = rmsd
        rmsd_matrix[j, i] = rmsd

    clusters = []
    for i in range(len(molecules)):
        cluster_found = False
        for cluster in clusters:
            if all(rmsd_matrix[i, j] < 0.5 for j in cluster):
                cluster.append(i)
                cluster_found = True
                break
        if not cluster_found:
            clusters.append([i])

    return clusters


def read_energy_coords_file(file_path):
    """
    Read energy and coordinate information from a file.

    Args:
        file_path (str): The path to the file.

    Returns:
        Tuple: A tuple containing the energy values, coordinates, and atom symbols.
    """
    all_energies = []
    all_coords = []
    all_atoms = []
    with open(file_path, 'r') as f:
        lines = f.readlines()
        i = 0
        while i < len(lines):
            # read energy value from line starting with "energy:"
            if len(lines[i].split()) == 1 and lines[i+1].strip().startswith("energy:"):
                energy_line = lines[i+1].strip()
                energy_value = float(energy_line.split()[1])
                all_energies.append(energy_value)
                i += 2
            else:
                raise ValueError(f"Unexpected line while reading energy value: {energy_line}")
            # read coordinates and symbols for next geometry
            coords = []
            atoms = []
            while i < len(lines) and len(lines[i].split()) != 1:
                atoms.append(lines[i].split()[0])
                coords.append(np.array(list(map(float,lines[i].split()[1:]))))
                i += 1

            all_coords.append(np.array(coords))
            all_atoms.append(atoms)
    return np.array(all_energies), all_coords, all_atoms


def determine_potential(all_coords, constraints, force_constant):
    """
    Determine the potential energy for a set of coordinates based on distance constraints and a force constant.

    Args:
        all_coords (list): A list of coordinate arrays.
        constraints (dict): A dictionary specifying the atom index pairs and their corresponding distances.
        force_constant (float): The force constant to apply to the constraints.

    Returns:
        list: A list of potential energy values.
    """
    potentials = []
    for coords in all_coords:
        potential = 0
        dist_matrix = distance_matrix(coords, coords)
        for key, val in constraints.items():
            actual_distance = dist_matrix[key[0], key[1]] - val
            potential += force_constant * angstrom_to_bohr(actual_distance) ** 2
        potentials.append(potential)

    return potentials
                

def angstrom_to_bohr(distance_angstrom):
    """
    Convert distance in angstrom to bohr.

    Args:
        distance_angstrom (float): Distance in angstrom.

    Returns:
        float: Distance in bohr.
    """
    return distance_angstrom * 1.88973


def get_optimal_distances(smiles, mapnum_dict, bonds, solvent=None, charge=0):
    """
    Calculate the optimal distances for a set of bonds in a molecule.

    Args:
        smiles (str): SMILES representation of the molecule.
        mapnum_dict (dict): Dictionary mapping atom map numbers to atom indices.
        bonds (list): List of bond strings.
        solvent (str, optional): Name of the solvent. Defaults to None.
        charge (int, optional): Charge of the molecule. Defaults to 0.

    Returns:
        dict: Dictionary mapping bond indices to their corresponding optimal distances.
    """
    mols = [Chem.MolFromSmiles(smi, ps) for smi in smiles.split('.')]
    owning_mol_dict = {}
    for idx, mol in enumerate(mols):
        for atom in mol.GetAtoms():
            owning_mol_dict[atom.GetAtomMapNum()] = idx

    optimal_distances = {}

    for bond in bonds:
        i, j, _ = map(int, bond.split('-'))
        idx1, idx2 = mapnum_dict[i], mapnum_dict[j]
        if owning_mol_dict[i] == owning_mol_dict[j]:
            mol = copy.deepcopy(mols[owning_mol_dict[i]])
        else:
            raise KeyError
    
        mol_dict = {atom.GetAtomMapNum(): atom.GetIdx() for atom in mol.GetAtoms()}
        [atom.SetAtomMapNum(0) for atom in mol.GetAtoms()]

        # detour needed to avoid reordering of the atoms by autodE
        get_conformer(mol)
        write_xyz_file_from_mol(mol, 'tmp.xyz')

        charge = Chem.GetFormalCharge(mol)

        if solvent is not None:
            ade_rmol = ade.Molecule('tmp.xyz', name='tmp', charge=charge, solvent_name=solvent)
        else:
            ade_rmol = ade.Molecule('tmp.xyz', name='tmp', charge=charge)
        ade_rmol.populate_conformers(n_confs=1)

        ade_rmol.conformers[0].optimise(method=xtb)
        dist_matrix = distance_matrix(ade_rmol.coordinates, ade_rmol.coordinates)
        current_bond_length = dist_matrix[mol_dict[i], mol_dict[j]]

        optimal_distances[idx1, idx2] = current_bond_length
    
    return optimal_distances


def prepare_smiles(smiles):
    """
    Prepare SMILES representation of a molecule.

    Args:
        smiles (str): SMILES representation of the molecule.

    Returns:
        str: Prepared SMILES representation.
    """
    mol = Chem.MolFromSmiles(smiles, ps)
    if '[H' not in smiles:
        mol = Chem.AddHs(mol)
    if mol.GetAtoms()[0].GetAtomMapNum() != 1:
        [atom.SetAtomMapNum(atom.GetIdx() + 1) for atom in mol.GetAtoms()]

    return Chem.MolToSmiles(mol)


def get_active_bonds(reactant_mol, product_mol):
    """
    Get the active bonds (formed and broken) between two molecules.

    Args:
        reactant_mol (Chem.Mol): Reactant molecule.
        product_mol (Chem.Mol): Product molecule.

    Returns:
        tuple: A tuple containing two sets:
            - Formed bonds (set of bond strings).
            - Broken bonds (set of bond strings).
    """
    reactant_bonds = get_bonds(reactant_mol)
    product_bonds = get_bonds(product_mol)

    formed_bonds = product_bonds - reactant_bonds
    broken_bonds = reactant_bonds - product_bonds

    return formed_bonds, broken_bonds


def get_bonds(mol):
    """
    Get the bond strings of a molecule.

    Args:
        mol (Chem.Mol): Molecule.

    Returns:
        set: Set of bond strings.
    """
    bonds = set()
    for bond in mol.GetBonds():
        atom_1 = mol.GetAtomWithIdx(bond.GetBeginAtomIdx()).GetAtomMapNum()
        atom_2 = mol.GetAtomWithIdx(bond.GetEndAtomIdx()).GetAtomMapNum()
        num_bonds = round(bond.GetBondTypeAsDouble())

        if atom_1 < atom_2:
            bonds.add(f'{atom_1}-{atom_2}-{num_bonds}')
        else:
            bonds.add(f'{atom_2}-{atom_1}-{num_bonds}')

    return bonds


def get_conformer(mol):
    """
    Generate and optimize a conformer of a molecule.

    Args:
        mol (Chem.Mol): Molecule.

    Returns:
        Chem.Mol: Molecule with optimized conformer.
    """
    AllChem.EmbedMolecule(mol, AllChem.ETKDG())
    AllChem.UFFOptimizeMolecule(mol)

    mol.GetConformer()

    return mol


def get_distance(coord1, coord2):
    """
    Calculate the Euclidean distance between two sets of coordinates.

    Args:
        coord1 (numpy.array): Coordinates of the first point.
        coord2 (numpy.array): Coordinates of the second point.

    Returns:
        float: Euclidean distance.
    """
    return np.sqrt(np.sum((coord1 - coord2) ** 2))


def write_xyz_file_from_mol(mol, filename):
    """
    Write a molecule's coordinates to an XYZ file.

    Args:
        mol (Chem.Mol): Molecule.
        filename (str): Name of the output XYZ file.
    """
    conformer = mol.GetConformer()
    coords = conformer.GetPositions()

    with open(filename, "w") as f:
        f.write(str(mol.GetNumAtoms()) + "\n")
        f.write("test \n")
        for i in range(mol.GetNumAtoms()):
            atom = mol.GetAtomWithIdx(i)
            symbol = atom.GetSymbol()
            x, y, z = coords[i]
            f.write(f"{symbol} {x:.6f} {y:.6f} {z:.6f}\n")
