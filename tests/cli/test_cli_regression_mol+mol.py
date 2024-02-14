"""This tests the CLI functionality of training and predicting a regression model on a multi-molecule.
"""

import pytest

from chemprop.cli.main import main

pytestmark = pytest.mark.CLI


@pytest.fixture
def data_path(data_dir):
    return (
        str(data_dir / "regression" / "mol+mol" / "mol+mol.csv"),
        (
            str(data_dir / "regression" / "mol+mol" / "features_0.npz"),
            str(data_dir / "regression" / "mol+mol" / "features_1.npz"),
        ),
        (
            str(data_dir / "regression" / "mol+mol" / "atom_features_0.npz"),
            str(data_dir / "regression" / "mol+mol" / "atom_features_1.npz"),
        ),
        (
            str(data_dir / "regression" / "mol+mol" / "bond_features_0.npz"),
            str(data_dir / "regression" / "mol+mol" / "bond_features_1.npz"),
        ),
        (
            str(data_dir / "regression" / "mol+mol" / "atom_descriptors_0.npz"),
            str(data_dir / "regression" / "mol+mol" / "atom_descriptors_1.npz"),
        ),
    )


@pytest.fixture
def model_path(data_dir):
    return str(data_dir / "example_model_v2_regression_mol+mol.pt")


def test_train_quick(monkeypatch, data_path):
    input_path, features_path, atom_features_path, bond_features_path, atom_descriptors_path = data_path
    args = [
        "chemprop",
        "train",
        "-i",
        data_path,
        "--smiles-columns",
        "smiles",
        "solvent",
        "--epochs",
        "1",
        "--num-workers",
        "0",
        "--features-path",
        features_path,
        "--atom-features-path",
        atom_features_path,
        "--bond-features-path",
        bond_features_path,
        "--atom-descriptors-path",
        atom_descriptors_path,
    ]

    with monkeypatch.context() as m:
        m.setattr("sys.argv", args)
        main()


def test_predict_quick(monkeypatch, data_path, model_path):
    args = [
        "chemprop",
        "predict",
        "-i",
        data_path,
        "--smiles-columns",
        "smiles",
        "solvent",
        "--model-path",
        model_path,
    ]

    with monkeypatch.context() as m:
        m.setattr("sys.argv", args)
        main()


def test_train_output_structure(monkeypatch, data_path, tmp_path):
    args = [
        "chemprop",
        "train",
        "-i",
        data_path,
        "--smiles-columns",
        "smiles",
        "solvent",
        "--epochs",
        "1",
        "--num-workers",
        "0",
        "--save-dir",
        str(tmp_path),
        "--save-smiles-splits",
    ]

    with monkeypatch.context() as m:
        m.setattr("sys.argv", args)
        main()

    assert (tmp_path / "model.pt").exists()
    assert (tmp_path / "chkpts" / "last.ckpt").exists()
    assert (tmp_path / "tb_logs" / "lightning_logs" / "version_0").exists()
    assert (tmp_path / "train_smiles.csv").exists()
